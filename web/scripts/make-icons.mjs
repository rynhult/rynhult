// Rynhult icons are approved brand-kit exports, not generated from the UI palette.
// This former generator now validates the exports without overwriting their artwork.
// Replace these files only with approved exports; see decision 0021.
import { readFileSync } from "node:fs";
const files = [["icon-192.png", 192], ["icon-512.png", 512],
  ["icon-maskable-512.png", 512], ["apple-touch-icon.png", 180]];
for (const [name, size] of files) {
  const bytes = readFileSync(new URL(`../public/icons/${name}`, import.meta.url));
  if (bytes.toString("hex", 0, 8) !== "89504e470d0a1a0a" ||
      bytes.readUInt32BE(16) !== size || bytes.readUInt32BE(20) !== size) {
    throw new Error(`Invalid approved icon: ${name}`);
  }
}
for (const name of ["favicon.svg", "favicon.ico", "rynhult-mark-light.svg", "rynhult-mark-dark.svg"]) {
  if (!readFileSync(new URL(`../public/${name}`, import.meta.url)).length) throw new Error(`Empty asset: ${name}`);
}
console.log("Approved Rynhult brand exports validated; artwork retained unchanged.");
