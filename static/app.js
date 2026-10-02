// fuaim — the interface script. Standard, no dependencies.
//
// Not implemented yet: this scaffold fetches nothing until the fixture index
// exists. The real script reads /config.json, lists the index's assets as
// waveform cards, and wires up the filters and the search box.

const library = document.getElementById("library");
const empty = library.querySelector(".empty");

fetch("/config.json")
  .then((r) => r.json())
  .then((config) => {
    empty.textContent =
      "fuaim " +
      (config.index_version ? "index v" + config.index_version : "scaffold") +
      " — no assets yet.";
  })
  .catch(() => {
    empty.textContent = "No index to show yet.";
  });
