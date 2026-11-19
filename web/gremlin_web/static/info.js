// The header's info panel (templates/info.html). The panel sits beside the page, which reflows
// around it (site.css), so the demo stays usable while it is open.

const toggle = document.querySelector("[data-info-toggle]");
const info = document.querySelector("[data-info]");

// Phones show the panel as a bottom sheet; the page keeps room for it below.
new ResizeObserver(() =>
  document.documentElement.style.setProperty("--info-h", `${info.offsetHeight}px`),
).observe(info);

const setOpen = (open) => {
  info.hidden = !open;
  document.documentElement.toggleAttribute("data-info-open", open);
  toggle.setAttribute("aria-expanded", String(open));
};

toggle.addEventListener("click", () => setOpen(info.hidden));
info.querySelector("[data-info-close]").addEventListener("click", () => {
  setOpen(false);
  toggle.focus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !info.hidden) {
    setOpen(false);
    toggle.focus();
  }
});
