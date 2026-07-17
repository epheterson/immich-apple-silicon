#!/usr/bin/env bash
# Render the Homebrew formula. SINGLE SOURCE OF TRUTH used by both
# update-homebrew.yml (release) and the ci.yml formula-check job, so the formula
# is validated on every PR, not first exercised when users install it (#17,#105).
# WARNING: this is an UNQUOTED heredoc. No backticks or $( ) in the body (command
# substitution corrupts the formula); shell vars are ${VAR}, Ruby is #{...}.
# Required env: OUT SRC_URL SRC_SHA ML_URL ML_SHA REPO OWNER VERSION
set -euo pipefail
: "${OUT:?} ${SRC_URL:?} ${SRC_SHA:?} ${ML_URL:?} ${ML_SHA:?} ${REPO:?} ${OWNER:?} ${VERSION:?}"

# Native Swift ML engine bundle. Optional: when NATIVE_URL is unset (e.g. the CI
# formula-check job) the formula renders without it and the accelerator uses the
# Python venv. When set (release) it installs the prebuilt ad-hoc-signed bundle.
# The ML models (~740MB) are NOT fetched at install time: the accelerator
# downloads them once in the background on first native start and caches them in
# ~/.cache, so installs stay fast, upgrades don't re-download, and a flaky network
# never blocks the install (the venv covers ML until the models arrive).
NATIVE_RESOURCE=""
NATIVE_INSTALL=""
if [ -n "${NATIVE_URL:-}" ]; then
  : "${NATIVE_SHA:?}"
  NATIVE_RESOURCE="
  resource \"native_ml\" do
    url \"${NATIVE_URL}\"
    sha256 \"${NATIVE_SHA}\"
  end
"
  NATIVE_INSTALL="    resource(\"native_ml\").stage do
      (libexec/\"native-ml\").install Dir[\"*\"]
    end
"
fi

cat > "$OUT" << EOF
class ImmichAccelerator < Formula
  desc "Run Immich compute natively on Apple Silicon"
  homepage "https://github.com/${REPO}"
  url "${SRC_URL}"
  sha256 "${SRC_SHA}"
  license "MIT"

  resource "ml" do
    url "${ML_URL}"
    sha256 "${ML_SHA}"
  end
${NATIVE_RESOURCE}
  depends_on :macos
  depends_on arch: :arm64
  # node@22 is the keg-only LTS that satisfies Immich's
  # engines.node pin. The default node formula tracks
  # mainline (currently 25.x) which breaks sharp's native
  # addons with NODE_MODULE_VERSION mismatches.
  depends_on "node@22"
  depends_on "vips"
  depends_on "libpq"
  depends_on "python@3.11"
  # GNU gzip for gzip --rsyncable. Apple's BSD gzip does
  # not support that flag, and Immich's database-backup
  # service pipes pg_dump stdout through it.
  depends_on "gzip"

  def install
    libexec.install Dir["*"]
    resource("ml").stage do
      (libexec/"ml").install Dir["*"]
    end
${NATIVE_INSTALL}    # Wrapper uses the ML venv Python so the CLI inherits its
    # third-party deps (fastapi, uvicorn - required by the
    # dashboard and already pinned in ml/requirements.txt).
    # Prevents ModuleNotFoundError on fresh installs where
    # Homebrew's python3.11 has no extra packages. (Issue #17.)
    (bin/"immich-accelerator").write <<~SH
      #!/bin/bash
      VENV_PY="#{libexec}/ml/venv/bin/python3.11"
      if [ ! -x "\\\$VENV_PY" ]; then
        echo "immich-accelerator: ML venv missing or broken (expected: \\\$VENV_PY)." >&2
        echo "This usually means post_install failed during brew install/upgrade." >&2
        echo "Fix with: brew reinstall immich-accelerator" >&2
        exit 1
      fi
      # Don't write .pyc into the Cellar. Python would otherwise
      # create __pycache__ next to the installed modules; if the CLI
      # is ever run as root (sudo, or a root service) those files are
      # root-owned and break "brew cleanup". (Issue #86.)
      export PYTHONDONTWRITEBYTECODE=1
      export PYTHONPATH="#{libexec}:\\\$PYTHONPATH"
      cd "#{libexec}"
      exec "\\\$VENV_PY" -m immich_accelerator "\\\$@"
    SH
  end

  def post_install
    # ML venv in post_install avoids Homebrew dylib fixup on
    # Rust-compiled Python extensions (pydantic_core, tokenizers).
    # The CLI wrapper also runs through this venv - its existence
    # is load-bearing for every subcommand, not just ML.
    ml_dir = libexec/"ml"
    venv_py = ml_dir/"venv/bin/python3.11"
    system Formula["python@3.11"].opt_bin/"python3.11", "-m", "venv", ml_dir/"venv"
    # The ML deps are a large download (torch, pulled in by mlx_clip, is a
    # few hundred MB), so a flaky connection is the common install failure
    # (issues #17, #105). Retry once so a transient blip self-recovers; a
    # deterministic failure still raises on the second try.
    tries = 0
    begin
      tries += 1
      system venv_py, "-m", "pip", "install", "-r", ml_dir/"requirements.txt"
    rescue
      retry if tries < 2
      raise
    end
    # Fail the install LOUDLY if the venv still lacks what the CLI, dashboard
    # and ML service need, instead of shipping a venv that exists but is
    # missing deps and only errors at runtime with ModuleNotFoundError
    # (issues #17, #105). system aborts on non-zero, so a broken install is
    # visible and "brew reinstall immich-accelerator" fixes it.
    verify_ml_venv venv_py
  end

  # Single source of truth for "the venv has what the CLI, dashboard and ML
  # service need." Called at install time (to fail a broken install loudly)
  # and from brew test. Imports the load-bearing packages and builds the
  # dashboard app, so a partial pip install (missing fastapi/uvicorn, or a
  # broken compiled mlx.core / torch via mlx_clip) is caught rather than
  # crashing at runtime (#17, #105). NOTE: no backticks in this heredoc,
  # they are command substitution and corrupt the generated formula. Uses
  # import mlx.core, not bare import mlx (an empty namespace that imports
  # even when the compiled extension is missing).
  def verify_ml_venv(venv_py)
    system venv_py, "-c", "import sys; sys.path.insert(0, '#{libexec}'); import fastapi, uvicorn; import mlx.core, mlx.nn; import mlx_clip; from immich_accelerator.dashboard import create_app; create_app({'version':'test','immich_url':'http://x','api_key':''})"
  end

  def caveats
    <<~EOS
      To get started:
        immich-accelerator setup

      Homebrew 5.1.15+ silently skips untrusted taps during upgrades.
      So future releases reach you, run once:
        brew trust ${OWNER}/immich-accelerator
    EOS
  end

  service do
    run [bin/"immich-accelerator", "watch"]
    keep_alive true
    log_path var/"log/immich-accelerator.log"
    error_log_path var/"log/immich-accelerator-error.log"
  end

  test do
    # --version exits before lazy third-party imports load, so
    # it's not enough on its own. Force-load the dashboard app
    # so we catch ModuleNotFoundError on fastapi/uvicorn at
    # brew audit / brew test time instead of in the wild.
    assert_match "immich-accelerator", shell_output("#{bin}/immich-accelerator --version")
    verify_ml_venv "#{libexec}/ml/venv/bin/python3.11"
  end
end
EOF
