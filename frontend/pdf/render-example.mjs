// Render the PDF template against the committed sample briefing to a real PDF,
// so the worked example can be reviewed without a browser. Dev tooling only.
import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const briefing = readFileSync(resolve(__dirname, "../data/sample-briefing.json"), "utf8");
const template = resolve(__dirname, "briefing-pdf.html");
const out = process.argv[2] || resolve(__dirname, "example-briefing.pdf");

const browser = await chromium.launch({
  executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
});
const page = await browser.newPage();
// Inject the briefing before the template's boot() runs.
await page.addInitScript((data) => { window.__BRIEFING__ = JSON.parse(data); }, briefing);
await page.goto("file://" + template, { waitUntil: "networkidle" });
await page.emulateMedia({ media: "print" });
await page.pdf({
  path: out,
  format: "Letter",
  printBackground: true,
  margin: { top: "14mm", bottom: "20mm", left: "14mm", right: "14mm" },
});
await browser.close();
console.log("wrote", out);
