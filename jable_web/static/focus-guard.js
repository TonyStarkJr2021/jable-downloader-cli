const guardedTextFields = Array.from(
  document.querySelectorAll("[data-mobile-focus-guard]")
);
const useMobileFocusGuard = window.matchMedia(
  "(hover: none) and (pointer: coarse)"
).matches;
const activatedTextFields = new WeakSet();

function lockTextField(field) {
  activatedTextFields.delete(field);
  field.readOnly = true;
  if (document.activeElement === field) field.blur();
}

function unlockTextField(field) {
  activatedTextFields.add(field);
  field.readOnly = false;
}

function prepareTextFieldsForPage() {
  guardedTextFields.forEach((field) => {
    lockTextField(field);
    if (!useMobileFocusGuard) unlockTextField(field);
  });
}

function lockAllTextFields() {
  guardedTextFields.forEach(lockTextField);
}

guardedTextFields.forEach((field) => {
  const unlock = () => unlockTextField(field);
  field.addEventListener("pointerdown", unlock);
  field.addEventListener("touchstart", unlock, { passive: true });
  field.addEventListener("mousedown", unlock);
  field.addEventListener("keydown", unlock);
  field.addEventListener("focus", () => {
    if (useMobileFocusGuard && !activatedTextFields.has(field)) field.blur();
  });
});

// Let physical-keyboard users tab into fields without weakening the mobile
// reload guard. Software-keyboard focus restoration does not emit a Tab key.
document.addEventListener("keydown", (event) => {
  if (event.key === "Tab") guardedTextFields.forEach(unlockTextField);
});

// Mobile browsers may restore the last focused control after pageshow. Every
// guarded field starts read-only and is unlocked only by a user interaction.
prepareTextFieldsForPage();
window.addEventListener("pageshow", prepareTextFieldsForPage);
window.addEventListener("pagehide", lockAllTextFields);
window.addEventListener("beforeunload", lockAllTextFields);
