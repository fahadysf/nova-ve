#!/usr/bin/env bash
set -euo pipefail

removed_validation_dir="site/development/ci/fy-lab-validation"
mkdir -p "${removed_validation_dir}"
cat > "${removed_validation_dir}/index.html" <<'HTML'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="robots" content="noindex">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Removed Validation Page - nova-ve</title>
  </head>
  <body>
    <main>
      <h1>Removed Validation Page</h1>
      <p>This page has been removed. Private environment-specific validation runbooks do not belong in public documentation.</p>
    </main>
  </body>
</html>
HTML
