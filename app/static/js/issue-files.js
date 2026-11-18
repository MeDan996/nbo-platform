/* Attachments on the issue forms.
 *
 * - Paste a screenshot into the description box and it is attached.
 * - Anything chosen or pasted is shown as a preview with a remove button.
 * Files live in the form's own file input, so submitting needs no extra code.
 */
(function () {
  var input = document.getElementById("files");
  var box = document.getElementById("description");
  var list = document.getElementById("file-previews");
  var note = document.getElementById("file-note");
  if (!input || !list || typeof DataTransfer === "undefined") return;

  var maxFiles = parseInt(input.dataset.maxFiles, 10) || 5;
  var maxBytes = (parseInt(input.dataset.maxMb, 10) || 8) * 1024 * 1024;
  var files = [];
  var urls = [];
  var pastedCount = 0;  // keeps pasted file names unique within the page

  function say(text) { if (note) { note.textContent = text || ""; note.hidden = !text; } }

  function sync() {
    var dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    input.files = dt.files;
    urls.forEach(function (u) { URL.revokeObjectURL(u); });
    urls = [];
    list.innerHTML = "";
    files.forEach(function (file, index) {
      var item = document.createElement("div");
      item.className = "file-preview";
      if (file.type.indexOf("image/") === 0 && file.type !== "image/heic") {
        var img = document.createElement("img");
        var url = URL.createObjectURL(file);
        urls.push(url);
        img.src = url;
        img.alt = file.name;
        item.appendChild(img);
      } else {
        var chip = document.createElement("span");
        chip.className = "badge badge-outline";
        chip.textContent = "\uD83D\uDCCE " + file.name;
        item.appendChild(chip);
      }
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "file-remove";
      remove.setAttribute("aria-label", input.dataset.msgRemove || "Remove");
      remove.textContent = "\u00D7";
      remove.addEventListener("click", function () {
        files.splice(index, 1);
        say("");
        sync();
      });
      item.appendChild(remove);
      list.appendChild(item);
    });
  }

  function add(newFiles) {
    say("");
    newFiles.forEach(function (file) {
      if (files.length >= maxFiles) { say((input.dataset.msgTooMany || "Too many files.").replace("{count}", maxFiles)); return; }
      if (file.size > maxBytes) { say((input.dataset.msgTooBig || "File too large.").replace("{size}", maxBytes / 1048576)); return; }
      files.push(file);
    });
    sync();
  }

  // Choosing files in the picker replaces the earlier choice, as a file input normally does.
  input.addEventListener("change", function () {
    if (input.files.length === files.length && Array.prototype.every.call(input.files, function (f, i) { return f === files[i]; })) return;
    files = [];
    add(Array.prototype.slice.call(input.files));
  });

  // Pasting a screenshot: attach it when the clipboard holds an image and no text.
  if (box) {
    box.addEventListener("paste", function (event) {
      var data = event.clipboardData;
      if (!data) return;
      var images = [];
      Array.prototype.forEach.call(data.items || [], function (item) {
        if (item.kind === "file" && item.type.indexOf("image/") === 0) {
          var file = item.getAsFile();
          if (file) images.push(file);
        }
      });
      if (!images.length || data.getData("text/plain")) return;
      event.preventDefault();
      var stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
      add(images.map(function (file, i) {
        var ext = ({ "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" })[file.type] || "png";
        pastedCount += 1;
        var name = !file.name || /^image\.\w+$/i.test(file.name) ? "screenshot-" + stamp + "-" + pastedCount + "." + ext : file.name;
        return new File([file], name, { type: file.type });
      }));
    });
  }
})();
