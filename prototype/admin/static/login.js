document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  try {
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("username").value, password: document.getElementById("password").value }) });
    const j = await r.json();
    if (!r.ok) { err.textContent = j.error || "ログインできませんでした"; return; }
    const next = new URLSearchParams(location.search).get("next");
    location.href = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  } catch (x) { err.textContent = "サーバーに接続できませんでした"; }
});
