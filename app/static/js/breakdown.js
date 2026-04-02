/**
 * breakdown.js – Breakdown page interactivity
 *
 * Rotates the expand arrow icon when a detail row is toggled open/closed.
 */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-bs-toggle='collapse']").forEach((trigger) => {
    const targetId = trigger.getAttribute("data-bs-target");
    const targetEl = document.querySelector(targetId);
    if (!targetEl) return;

    const arrow = trigger.querySelector("td:first-child");

    targetEl.addEventListener("show.bs.collapse", () => {
      if (arrow) arrow.textContent = "▼";
    });
    targetEl.addEventListener("hide.bs.collapse", () => {
      if (arrow) arrow.textContent = "▶";
    });
  });
});
