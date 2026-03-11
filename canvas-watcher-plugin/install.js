/**
 * Installs the Canvas Watcher plugin into the current Obsidian vault.
 *
 * Usage (run from anywhere inside the vault):
 *   node canvas-watcher-plugin/install.js
 */
const fs = require("fs");
const path = require("path");

// Walk up from this script's directory to find .obsidian
function findVaultRoot(startDir) {
  let dir = startDir;
  while (true) {
    if (fs.existsSync(path.join(dir, ".obsidian"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null; // reached filesystem root
    dir = parent;
  }
}

const scriptDir = __dirname;
const vaultRoot = findVaultRoot(scriptDir);

if (!vaultRoot) {
  console.error("Could not find .obsidian folder. Are you inside an Obsidian vault?");
  process.exit(1);
}

const targetDir = path.join(vaultRoot, ".obsidian", "plugins", "canvas-watcher");
const sourceFiles = ["main.js", "manifest.json"];

// Create target directory
fs.mkdirSync(targetDir, { recursive: true });

// Copy files
for (const file of sourceFiles) {
  const src = path.join(scriptDir, file);
  const dst = path.join(targetDir, file);
  fs.copyFileSync(src, dst);
  console.log(`  ${file} -> ${path.relative(vaultRoot, dst)}`);
}

console.log("\nCanvas Watcher plugin installed.");
console.log("Enable it in Obsidian: Settings > Community plugins > Canvas Watcher");
