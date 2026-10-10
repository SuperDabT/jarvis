// The stage's API, called from Python (school_stage.SchoolStage.call).
// Scenes register themselves by name; show() cross-fades to one.
const params = new URLSearchParams(location.search);
const motion = params.get("motion") !== "0";
const empty = document.getElementById("empty");
const sceneEl = document.getElementById("scene");
const scenes = {};          // name -> {mount(el, data, ctx) -> {destroy?()}}
let current = null;         // {name, instance}

if (!motion) document.body.classList.add("still");

function swap(show) {
  empty.classList.toggle("shown", !show);
  sceneEl.classList.toggle("shown", show);
}

window.stage = {
  motion,
  register(name, scene) { scenes[name] = scene; },
  has(name) { return name in scenes; },
  current() { return current ? current.name : null; },
  show(name, data) {
    const scene = scenes[name];
    if (!scene) { console.error(`no scene "${name}"`); return false; }
    if (current && current.instance && current.instance.destroy) current.instance.destroy();
    sceneEl.replaceChildren();
    current = { name, instance: scene.mount(sceneEl, data || {}, { motion }) };
    swap(true);
    return true;
  },
  clear() {
    if (current && current.instance && current.instance.destroy) current.instance.destroy();
    current = null;
    swap(false);
  },
};

requestAnimationFrame(() => document.body.classList.add("ready"));
