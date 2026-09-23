import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";

const dir = new URL("./", import.meta.url);
const manifest = JSON.parse(readFileSync(new URL("manifest.json", dir), "utf8"));

test("manifest is MV3 with a module service worker that exists", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.background, { service_worker: "background.js", type: "module" });
  assert.ok(existsSync(new URL("background.js", dir)));
  assert.ok(existsSync(new URL("lib.js", dir)));
});

test("manifest asks for webRequest only", () => {
  assert.deepEqual(manifest.permissions, ["webRequest"]);
  assert.equal(manifest.optional_permissions, undefined);
});

test("manifest host permissions are claude.ai and the local proxy only", () => {
  assert.deepEqual(manifest.host_permissions, ["https://claude.ai/*", "http://127.0.0.1:8787/*"]);
  assert.equal(manifest.optional_host_permissions, undefined);
});

test("manifest has no content scripts or page-facing surfaces", () => {
  for (const key of ["content_scripts", "web_accessible_resources", "externally_connectable", "action"]) {
    assert.equal(manifest[key], undefined, key);
  }
});
