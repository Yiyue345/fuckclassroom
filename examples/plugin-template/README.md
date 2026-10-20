# FuckClassroom minimal Python plugin template

A copyable, self-contained Plugin API v1 and Registry v1 publishing skeleton. This folder is a **template directory**, not a published plugin or a GitHub template repository.

## Files

~~~text
plugin.json                         Plugin metadata and compatibility
plugin.py                           GET /sample-plugin/hello route
scripts/build_registry_artifact.py  ZIP + SHA-256 packaging
tests/test_package.py               Publish contract regression tests
.github/workflows/ci.yml            PR/push packaging checks
.github/workflows/release.yml       Manual GitHub Release workflow
~~~

## 1. Create a repository from the folder

Copy **the contents** of this folder into the root of your own new GitHub repository, preserving .github/workflows. This repository should be public for normal public Registry distribution. Do not move the folder into a wrapper directory in the target repository.

Change:
- plugin.json: id, name, description, base version, app_requires, dependencies.
- plugin.py: PluginSpec id/name/requires, route URL, hooks.
- tests/test_package.py: route assertions if you change the example implementation.
- .github/workflows/ci.yml: sample beta version if changing the base version.

The plugin ID must be unique and follow /^[a-z][a-z0-9_-]{1,63}$/ (e.g. sample_plugin). The ID in plugin.json must equal PluginSpec.id.

This starter deliberately uses **in_process** execution and Plugin API v1, with core_ui as its only dependency. For independent process plugins, follow the complete Plugin RPC API documentation instead of just flipping execution to "process".

## 2. Test and build locally

Requires Python 3.11+; no third-party package is needed for package validation.

~~~bash
python -m unittest discover -s tests -v
python scripts/build_registry_artifact.py 0.1.0-beta.1
cd dist
sha256sum -c -- *.sha256
~~~

The builder produces:

~~~text
dist/sample_plugin-0.1.0-beta.1.zip
dist/sample_plugin-0.1.0-beta.1.zip.sha256
~~~

The ZIP contains exactly plugin.json and plugin.py at its **root**. It does not wrap them in a sample_plugin directory. When extending this starter to ship other modules or assets, explicitly add them to the packaging script and tests.

A ZIP verified by these tests is **not** a substitute for starting FuckClassroom and testing the enabled plugin's route.

## 3. Publish a GitHub Release

Push your repo to GitHub. Make sure Actions is enabled and the workflow has permissions to publish releases (Actions → General → Workflow permissions).

Run Actions → "Publish Registry v1 release" → Run workflow, with a version such as 0.1.0-beta.1. The workflow tests, packages, verifies SHA-256 and creates immutable tag v0.1.0-beta.1 and its GitHub Release containing .zip and .zip.sha256.

- Version must be SemVer; a suffix like -beta.1 makes the Release a prerelease.
- Never republish or modify the contents of an existing version. Increment the version and create a new Release instead.
- The packaged plugin.json version is set to the selected version, but its major.minor.patch must match the base version recorded in the source plugin.json.
- Signing is optional; this starter publishes unsigned artifacts. See the Registry publisher signing key / .zip.sig documentation for signed releases.

## 4. Register in FuckClassroom Registry

From the [Registry website repository](https://github.com/Yiyue345/f-classroom-plugin-website), install the Node/pnpm environment and register your plugin:

~~~bash
pnpm install --frozen-lockfile
pnpm registry:new-plugin
pnpm registry:import-release -- sample_plugin https://github.com/OWNER/REPO/releases/tag/v0.1.0-beta.1
pnpm registry:verify-releases
pnpm check
pnpm build
~~~

The new-plugin wizard requires an existing Publisher, and generates Registry metadata plus Chinese/English README drafts. Commit the Registry change for review. A draft stays invisible to the public Index until at least one real GitHub Release has been imported.

The imported artifact filename must be exactly <plugin-id>-<version>.zip. Registry validates the zip, hashes it, checks manifest/runtime fields and creates immutable version JSON. The optional .sha256 companion file is for human/CI checks: Registry hashes the real ZIP independently.

## 5. Install, enable and update

Open FuckClassroom → Plugins → Online plugins. Turn on Beta releases to see 0.1.0-beta.1. Install the plugin, then enable it separately (new installs remain disabled). Visit /sample-plugin/hello on the host to test the example route.

For a new release: update source code, increment the release SemVer, publish through Actions, import it into Registry, refresh the in-app listing, disable the plugin and dependent plugins before updating, then enable them again. Current updates do **not** automatically re-enable or hot-reload an active plugin; enabling triggers the existing Runtime reload path.

The full step-by-step guide is in the main project's docs/plugin-publishing.md and the plugin website Publishing page.
