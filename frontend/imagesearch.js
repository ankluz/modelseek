import { $, el, toast } from "./util.js";

export const IMAGE_SERVICES = [
  {
    id: "lens", label: "Google Lens",
    byUrl: (u) => "https://lens.google.com/uploadbyurl?url=" + encodeURIComponent(u),
    upload: { action: "https://lens.google.com/v3/upload", field: "encoded_image", kind: "file" },
  },
  {
    id: "yandex", label: "Яндекс Картинки",
    byUrl: (u) => "https://yandex.com/images/search?rpt=imageview&url=" + encodeURIComponent(u),
    upload: { action: "https://yandex.com/images/search?rpt=imageview", field: "upfile", kind: "file" },
  },
  {
    id: "bing", label: "Bing Visual Search",
    byUrl: (u) => "https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:" + encodeURIComponent(u),
    upload: { action: "https://www.bing.com/images/search?view=detailv2&iss=sbiupload", field: "imageBin", kind: "base64" },
  },
];

const MAX_SIDE = 1600;
let current = null;

async function shrink(file) {
  if (!file.type.startsWith("image/") || file.type === "image/gif") return file;
  const bmp = await createImageBitmap(file).catch(() => null);
  if (!bmp) return file;
  const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.size < 3_000_000) return file;
  const canvas = Object.assign(document.createElement("canvas"), {
    width: Math.round(bmp.width * scale), height: Math.round(bmp.height * scale) });
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.88));
  return blob ? new File([blob], "image.jpg", { type: "image/jpeg" }) : file;
}

const toBase64 = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(",")[1] || "");
  r.onerror = reject;
  r.readAsDataURL(file);
});

async function submitUpload(service, file) {
  const form = el("form", { method: "POST", enctype: "multipart/form-data", action: service.upload.action,
    target: "_blank", hidden: true });
  if (service.upload.kind === "base64") {
    form.append(el("input", { type: "hidden", name: service.upload.field, value: await toBase64(file) }));
  } else {
    const input = el("input", { type: "file", name: service.upload.field });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
  setTimeout(() => form.remove(), 1000);
}

export function searchByUrl(service, url) {
  window.open(service.byUrl(url), "_blank", "noopener");
}

function setFile(file) {
  if (!file) return;
  if (/\.(stl|3mf|obj|step|stp)$/i.test(file.name)) {
    toast("Это 3D-файл. Поиск по форме умеет Thangs — ссылка ниже.", 6000);
    $("#img-stl").hidden = false;
    return;
  }
  if (!file.type.startsWith("image/")) { toast("Нужна картинка: JPG, PNG, WebP…", 5000); return; }
  current = { file, url: null };
  const prev = $("#img-preview");
  prev.replaceChildren(el("img", { src: URL.createObjectURL(file), alt: "Выбранная картинка" }));
  prev.hidden = false;
  $("#img-url").value = "";
  paintButtons();
}

function paintButtons() {
  const ready = current && (current.file || current.url);
  $("#img-services").replaceChildren(...IMAGE_SERVICES.map((s) => el("button", {
    type: "button", class: "btn", disabled: !ready,
    onclick: async () => {
      if (current?.url) return searchByUrl(s, current.url);
      try { await submitUpload(s, await shrink(current.file)); }
      catch (e) { toast("Не удалось отправить: " + e.message, 6000); }
    },
  }, s.label)));
}

export function bindImageSearch() {
  const dlg = $("#img-dialog");
  $("#img-open").addEventListener("click", () => {
    current = null;
    $("#img-preview").hidden = true;
    $("#img-stl").hidden = true;
    $("#img-url").value = "";
    paintButtons();
    dlg.showModal();
  });
  $("#img-close").addEventListener("click", () => dlg.close());
  const file = $("#img-file");
  file.addEventListener("change", () => setFile(file.files[0]));
  const drop = $("#img-drop");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); setFile(e.dataTransfer.files[0]); });
  dlg.addEventListener("paste", (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); setFile(item.getAsFile()); }
  });
  $("#img-url").addEventListener("input", (e) => {
    const v = e.target.value.trim();
    current = /^https?:\/\//.test(v) ? { file: null, url: v } : null;
    $("#img-preview").hidden = !current;
    if (current) $("#img-preview").replaceChildren(el("img", { src: v, alt: "Картинка по ссылке", referrerpolicy: "no-referrer" }));
    paintButtons();
  });
}

export function openSimilarPopover(h, anchor) {
  const pop = $("#fav-pop");
  const close = () => { pop.hidden = true; document.removeEventListener("mousedown", outside, true); };
  const outside = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };
  pop.replaceChildren(el("div", { class: "pop-title" }, "Найти похожие по картинке"),
    ...IMAGE_SERVICES.map((s) => el("button", { type: "button", class: "pop-item",
      onclick: () => { close(); searchByUrl(s, h.thumbnail); } }, s.label)));
  const r = anchor.getBoundingClientRect();
  pop.hidden = false;
  const w = pop.offsetWidth, hgt = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
  pop.style.top = (r.bottom + hgt + 8 > window.innerHeight ? r.top - hgt - 6 : r.bottom + 6) + window.scrollY + "px";
  setTimeout(() => document.addEventListener("mousedown", outside, true));
  pop.onkeydown = (e) => { if (e.key === "Escape") close(); };
  pop.querySelector(".pop-item")?.focus();
}
