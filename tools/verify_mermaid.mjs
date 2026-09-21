#!/usr/bin/env node
/**
 * Mermaid Render Gate — parses every Mermaid block with the real Mermaid engine.
 *
 * The Python docs validator checks what regex can see: label quoting, diagram type,
 * palette contrast. It cannot tell whether a diagram actually renders. Grammar errors
 * that pass every textual rule — a semicolon inside sequence diagram text, an unbalanced
 * subgraph, an edge form the parser rejects — reach GitHub as a broken block.
 *
 * Usage:  node tools/verify_mermaid.mjs [path ...]     (default: repository root)
 * Exit:   0 when every diagram parses, 1 otherwise.
 *
 * Requires: npm install --no-save mermaid@11 jsdom
 */

import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";
import { JSDOM } from "jsdom";

const SKIP_DIRS = new Set([".git", "node_modules", "__pycache__", ".venv"]);

function installDom() {
  const dom = new JSDOM("<!DOCTYPE html><body></body>", { pretendToBeVisual: true });
  for (const key of ["window", "document", "Element", "SVGElement", "HTMLElement", "getComputedStyle"]) {
    const value = key === "window" ? dom.window : dom.window[key];
    Object.defineProperty(global, key, { value, configurable: true, writable: true });
  }
  Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true });
}

function collectMarkdown(target) {
  if (statSync(target).isFile()) return target.endsWith(".md") ? [target] : [];
  return readdirSync(target).flatMap((entry) =>
    SKIP_DIRS.has(entry) ? [] : collectMarkdown(join(target, entry)),
  );
}

function extractDiagrams(file) {
  const lines = readFileSync(file, "utf8").split("\n");
  const blocks = [];
  let start = -1;
  lines.forEach((line, index) => {
    if (start === -1 && line.trim().startsWith("```mermaid")) start = index;
    else if (start !== -1 && line.trim() === "```") {
      blocks.push({ line: start + 1, body: lines.slice(start + 1, index).join("\n") });
      start = -1;
    }
  });
  return blocks;
}

installDom();
const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false });

const targets = process.argv.slice(2).length ? process.argv.slice(2) : ["."];
const missing = targets.filter((target) => !existsSync(target));
for (const target of missing) {
  console.error(`MISSING ${target} — refusing to certify a path that does not exist.`);
}

let parsed = 0;
const failures = [];
for (const target of targets.filter((t) => existsSync(t))) {
  for (const file of collectMarkdown(target)) {
    for (const { line, body } of extractDiagrams(file)) {
      parsed += 1;
      try {
        await mermaid.parse(body);
      } catch (error) {
        failures.push({ file, line, message: String(error.message).split("\n")[0] });
      }
    }
  }
}

for (const { file, line, message } of failures) {
  console.error(`FAIL ${file}:${line} — ${message}`);
}
console.log(`${parsed} Mermaid diagrams parsed, ${failures.length} failed.`);
process.exit(failures.length || missing.length ? 1 : 0);
