// Render the PDF template against the committed sample briefing to a real PDF,
// so the worked example can be reviewed without a browser. Dev tooling only.
//
// Served over a throwaway localhost HTTP server (not file://) so the template's
// fetches — the posture-label config (data/display-config.json) and the logo —
// resolve exactly as they do in the deployed, single-origin PWA. Chromium blocks
// fetch() of file:// resources, which would otherwise leave the example showing
// raw engine labels instead of the approachable "Exposure" language.
import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join, extname } from "node:path";
import { createServer } from "node:http";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, ".."); // frontend/
const out = process.argv[2] || resolve(__dirname, "example-briefing.pdf");

// The live tool seeds app_version from /v1/health; for the worked example we
// inject a representative release so the masthead/footer show a real value.
const _b = JSON.parse(readFileSync(resolve(ROOT, "data/sample-briefing.json"), "utf8"));
_b.app_version = "0.5.0";
const briefing = JSON.stringify(_b);

const MIME = {
  ".html": "text/html", ".js": "text/javascript", ".json": "application/json",
  ".png": "image/png", ".css": "text/css", ".svg": "image/svg+xml",
};
const server = createServer((req, res) => {
  try {
    const body = readFileSync(join(ROOT, decodeURIComponent(req.url.split("?")[0])));
    res.setHeader("Content-Type", MIME[extname(req.url.split("?")[0])] || "application/octet-stream");
    res.end(body);
  } catch {
    res.statusCode = 404;
    res.end("not found");
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;

const browser = await chromium.launch({
  executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
});
const page = await browser.newPage();
// Inject the briefing before the template's boot() runs.
await page.addInitScript((data) => { window.__BRIEFING__ = JSON.parse(data); }, briefing);
await page.goto(`http://127.0.0.1:${port}/pdf/briefing-pdf.html`, { waitUntil: "networkidle" });
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
server.close();
console.log("wrote", out);
