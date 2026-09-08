// Embedded in the frontend PostSync Job; use the released image's Node runtime.
const checks = JSON.parse(process.env.SMOKE_CHECKS);
if (!Array.isArray(checks) || checks.length === 0) throw new Error("Empty smoke checks");
for (let attempt = 1; attempt <= 12; attempt++) {
  try {
    for (const probe of checks) {
      const response = await fetch(probe.url, {
        redirect: "error", signal: AbortSignal.timeout(5000),
      });
      if (response.status !== 200) throw new Error("HTTP " + response.status);
      const body = await response.text();
      if (!body.trim() || !body.toLowerCase().includes(probe.contains.toLowerCase())) {
        throw new Error("Unexpected frontend response");
      }
      console.log("PASS " + probe.url);
    }
    process.exit(0);
  } catch (error) {
    console.error("FAIL attempt=" + attempt + " (" + error.name + ")");
    if (attempt === 12) process.exit(1);
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
}
