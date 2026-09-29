export class ConfirmDialog {
  constructor() {
    this.version = 0;
    this.dialog = document.createElement("dialog");
    this.dialog.className = "confirm-dialog";
    this.dialog.innerHTML = `
      <form method="dialog">
        <h2></h2>
        <img alt="Prévia da música" hidden>
        <p></p>
        <div class="confirm-actions">
          <button type="submit" value="cancel" class="text-button">Cancelar</button>
          <button type="submit" value="confirm" class="danger">Remover música</button>
        </div>
      </form>`;
    document.body.append(this.dialog);
  }

  async open({ title, message, preview, confirmLabel = "Remover música" }) {
    const dialog = this.dialog;
    const version = ++this.version;
    dialog.querySelector("h2").textContent = title;
    dialog.querySelector("p").textContent = message;
    dialog.querySelector("button[value=confirm]").textContent = confirmLabel;
    const image = dialog.querySelector("img");
    image.hidden = true;
    image.removeAttribute("src");
    dialog.returnValue = "";
    dialog.showModal();
    if (preview) {
      Promise.resolve(preview).then((source) => {
        if (dialog.open && this.version === version && source) {
          image.src = source;
          image.hidden = false;
        }
      }).catch(() => {});
    }
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    });
  }
}