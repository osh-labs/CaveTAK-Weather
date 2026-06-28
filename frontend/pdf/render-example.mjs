// Render the PDF template against the committed sample briefing to a real PDF,
// so the worked example can be reviewed without a browser. Dev tooling only.
import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
// The live tool seeds app_version from /v1/health; for the worked example we
// inject a representative release so the masthead/footer show a real value.
const _b = JSON.parse(readFileSync(resolve(__dirname, "../data/sample-briefing.json"), "utf8"));
_b.app_version = "0.5.0";
const briefing = JSON.stringify(_b);
const template = resolve(__dirname, "briefing-pdf.html");
const out = process.argv[2] || resolve(__dirname, "example-briefing.pdf");

const browser = await chromium.launch({
  executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
});
const page = await browser.newPage();
// Inject the briefing before the template's boot() runs.
await page.addInitScript((data) => { window.__BRIEFING__ = JSON.parse(data); }, briefing);
await page.goto("file://" + template, { waitUntil: "networkidle" });
// The masthead logo <img> is added by render() after networkidle, so wait for
// it to finish decoding before printing (else the PDF captures an empty box).
await page.waitForFunction(() => {
  const im = document.querySelector(".brand__logo");
  return im && im.complete && im.naturalWidth > 0;
}, { timeout: 5000 }).catch(() => {});
await page.emulateMedia({ media: "print" });
// Let the template's @page rule own size + margins (preferCSSPageSize) so the
// fixed footer's page-area geometry matches what window.print() will produce.
await page.pdf({ path: out, printBackground: true, preferCSSPageSize: true });
await browser.close();
console.log("wrote", out);
