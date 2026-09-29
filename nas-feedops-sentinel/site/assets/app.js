// Sentinel story site: menu, section highlighting, reveal on scroll. No dependencies.
(() => {
  "use strict";

  // Year in the footer.
  const year = document.getElementById("year");
  if (year) year.textContent = String(new Date().getFullYear());

  // Small-screen menu.
  const menu = document.querySelector(".menu");
  const nav = document.getElementById("nav");
  const setMenu = (open) => {
    nav.classList.toggle("is-open", open);
    menu.setAttribute("aria-expanded", String(open));
    menu.setAttribute("aria-label", open ? "Close the section menu" : "Open the section menu");
  };
  if (menu && nav) {
    menu.addEventListener("click", () => setMenu(!nav.classList.contains("is-open")));
    nav.addEventListener("click", (e) => { if (e.target.closest("a")) setMenu(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
  }

  if (!("IntersectionObserver" in window)) return;

  // Highlight the section in view.
  const links = new Map([...document.querySelectorAll(".nav a")].map((a) => [a.getAttribute("href").slice(1), a]));
  const spy = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      links.forEach((a) => a.removeAttribute("aria-current"));
      const link = links.get(entry.target.id);
      if (link) link.setAttribute("aria-current", "true");
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  links.forEach((_, id) => { const s = document.getElementById(id); if (s) spy.observe(s); });

  // Reveal: only what starts below the fold is hidden, so nothing flashes and
  // nothing is hidden when this script does not run.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const reveal = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { entry.target.classList.remove("is-pending"); reveal.unobserve(entry.target); }
    }
  }, { rootMargin: "0px 0px -8% 0px" });
  document.querySelectorAll(".reveal").forEach((el) => {
    if (el.getBoundingClientRect().top > window.innerHeight) { el.classList.add("is-pending"); reveal.observe(el); }
  });
})();
