/* Runs the dashboard's own script and reads back what it painted.
 *
 * The page is a real browser file with no build step, so the honest way to check
 * what it does with a status event is to run that file, with the smallest DOM that
 * lets it load, and then read the elements back. A hand-written copy of the paint
 * logic here would agree with itself and prove nothing.
 *
 * Usage: node paint_status.mjs '<status payload json>'
 */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const elements = new Map();
const noop = () => {};

function element(id) {
  if (elements.has(id)) return elements.get(id);
  const node = {
    id,
    hidden: false,
    children: [],
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    style: {},
    dataset: {},
    getContext: () => new Proxy({}, { get: () => noop }),
    addEventListener: noop,
    removeEventListener: noop,
    setAttribute: noop,
    getAttribute: () => null,
    focus: noop,
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.unshift(child); return child; },
    replaceChildren() { this.children.length = 0; },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; },
    remove() {},
    removeAttribute: noop,
    closest: () => null,
    matches: () => false,
    scrollIntoView: noop,
    get offsetWidth() { return 400; },
    get offsetHeight() { return 200; },
    get offsetTop() { return 0; },
    get offsetLeft() { return 0; },
    get offsetParent() { return null; },
    get clientWidth() { return 400; },
    get clientHeight() { return 200; },
    get scrollTop() { return 0; },
    get scrollHeight() { return 0; },
    get nextElementSibling() { return null; },
    get previousElementSibling() { return null; },
    get parentElement() {
      // any element the script climbs to is just a box with a size
      return { getBoundingClientRect: () => ({ width: 400, height: 200, top: 0, left: 0 }),
               clientWidth: 400, clientHeight: 200, classList: { add: noop, remove: noop } };
    },
    get firstChild() { return this.children.length ? this.children[0] : null; },
    get textContent() { return this._text || ""; },
    set textContent(value) { this._text = String(value); this.children.length = 0; },
    get innerHTML() { return this._text || ""; },
    set innerHTML(value) { this._text = String(value); this.children.length = 0; },
    get width() { return 400; },
    get height() { return 200; },
      getBoundingClientRect: () => ({ width: 400, height: 200, top: 0, left: 0 }),
  };
  elements.set(id, node);
  return node;
}

globalThis.document = {
  getElementById: element,
  createElement: (tag) => element(`<${tag}:${Math.random()}>`),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  removeEventListener: noop,
  body: element("body"),
  documentElement: element("html"),
  hidden: false,
};
globalThis.window = { addEventListener: noop, removeEventListener: noop, ARCHY_TOKEN: "test-token" };
globalThis.EventSource = class { constructor() {} close() {} addEventListener() {} };
globalThis.requestAnimationFrame = noop;
globalThis.cancelAnimationFrame = noop;
globalThis.setInterval = noop;
globalThis.clearInterval = noop;
globalThis.setTimeout = (fn) => { return 0; };
globalThis.getComputedStyle = () => ({ color: "rgb(230, 230, 230)" });
globalThis.devicePixelRatio = 1;
globalThis.location = { search: "", href: "http://127.0.0.1:8731/", hash: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
if (!globalThis.navigator) globalThis.navigator = { userAgent: "node" };

const source = readFileSync(new URL("../hud/app.js", import.meta.url), "utf8");
vm.runInThisContext(source, { filename: "hud/app.js" });

if (typeof paintStatus !== "function") {
  console.error("app.js does not define paintStatus");
  process.exit(2);
}

paintStatus(JSON.parse(process.argv[2]));

const panel = element("status-panel");
const read = (id) => element(id).textContent;
console.log(JSON.stringify({
  hidden: panel.hidden,
  values: {
    model: read("model-active"),
    where: read("model-where"),
    mode: read("mode"),
    connectivity: read("connectivity"),
    local_model: read("local-model"),
    privacy_online: read("privacy-online"),
    waiting: read("waiting"),
  },
  events: element("status-events").children.map((child) => child.textContent),
}));
