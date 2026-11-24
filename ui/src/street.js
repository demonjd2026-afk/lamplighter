// The street: a small real-time 3D diorama of the watched system.
// Every service is a lamp on its own plinth with a little building behind it. The lamp's light is the
// service's health, the points of light on the wires are requests, and the mint drone is Lamplighter.
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const THEMES = {
  dark: { sky: 0x090d22, ground: 0x161c40, rim: 0x0d1230, grid: 0x2b3676, road: 0x1f2755, post: 0x8791c9, prop: 0x222a5e,
          roof: 0x2f3a7c, wire: 0x5d69b4, hemiSky: 0x5566cc, hemiGround: 0x090d22, hemi: 0.55, sun: 0x9fb4ff, sunPower: 0.5,
          lamp: 6.5, bloom: 0.85, threshold: 0.7, mint: 0x52f0c0, glass: 2.8, stars: true, dot: 0xffc766 },
  light: { sky: 0xdde7f5, ground: 0xffffff, rim: 0xaebcd8, grid: 0xc5d0e6, road: 0xcdd7ea, post: 0x2f3a5e, prop: 0xffffff,
           roof: 0x8fa3d1, wire: 0x7f8db0, hemiSky: 0xffffff, hemiGround: 0xb9c6de, hemi: 0.95, sun: 0xfff1d6, sunPower: 2.7,
           lamp: 1.4, bloom: 0.12, threshold: 0.95, mint: 0x0a9a78, glass: 1.2, stars: false, dot: 0xe07a00 },
};
const LAMP = new THREE.Color(0xffb547), EMBER = new THREE.Color(0xff3d62), OUT = new THREE.Color(0x39406e);
const HOME = new THREE.Vector3(-1.4, 1.3, 4.6);
const LANTERN_Y = 2.75;

function box(w, h, d, material, x, y, z, parent, shadow = true) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  mesh.position.set(x, y, z); mesh.castShadow = shadow; mesh.receiveShadow = true; parent.add(mesh);
  return mesh;
}

export class Street {
  constructor(canvas, tags, leaders, reducedMotion) {
    this.canvas = canvas; this.tags = tags; this.leaders = leaders; this.calm = reducedMotion;
    this.nodes = new Map(); this.edges = []; this.signature = ""; this.root = null; this.pointer = new THREE.Vector2();
    this.theme = THEMES.dark; this.clock = new THREE.Clock(); this.rings = []; this.particles = []; this.focus = new THREE.Vector3(0, -1.2, 0);
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.shadowMap.enabled = true; this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.scene = new THREE.Scene(); this.scene.fog = new THREE.Fog(0x000000, 22, 46);
    this.camera = new THREE.PerspectiveCamera(33, 2, 0.1, 120);
    this.world = new THREE.Group(); this.scene.add(this.world);       // rebuilt when the topology changes
    this.materials = {
      ground: new THREE.MeshStandardMaterial({ roughness: 0.92 }), rim: new THREE.MeshStandardMaterial({ roughness: 1 }),
      road: new THREE.MeshStandardMaterial({ roughness: 0.95 }), post: new THREE.MeshStandardMaterial({ roughness: 0.45, metalness: 0.6 }),
      prop: new THREE.MeshStandardMaterial({ roughness: 0.8 }), roof: new THREE.MeshStandardMaterial({ roughness: 0.7 }),
      wire: new THREE.MeshBasicMaterial(), ok: new THREE.MeshBasicMaterial({ color: 0xffc766, toneMapped: false }),
      bad: new THREE.MeshBasicMaterial({ color: 0xff4d6d, toneMapped: false }),
    };
    this.hemi = new THREE.HemisphereLight(); this.scene.add(this.hemi);
    this.sun = new THREE.DirectionalLight(); this.sun.position.set(-7, 13, 8); this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(2048, 2048); Object.assign(this.sun.shadow.camera, { left: -13, right: 13, top: 13, bottom: -13, near: 1, far: 40 });
    this.sun.shadow.bias = -0.0006; this.scene.add(this.sun);
    this.buildStatic();
    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.8, 0.55, 0.72); this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
    canvas.addEventListener("pointermove", (event) => {
      const rect = canvas.getBoundingClientRect();
      this.pointer.set(((event.clientX - rect.left) / rect.width - 0.5) * 2, ((event.clientY - rect.top) / rect.height - 0.5) * 2);
    });
    canvas.addEventListener("pointerleave", () => this.pointer.set(0, 0));
    this.setTheme("dark");
    this.renderer.setAnimationLoop(() => this.frame());
  }

  buildStatic() {
    const m = this.materials;
    const island = new THREE.Mesh(new THREE.CylinderGeometry(10.5, 10.1, 0.6, 96), [m.rim, m.ground, m.rim]);
    island.position.y = -0.3; island.receiveShadow = true; this.scene.add(island);
    this.grid = new THREE.PolarGridHelper(10.3, 12, 7, 96); this.grid.position.y = 0.012; this.scene.add(this.grid);
    const starPositions = [];
    for (let i = 0; i < 420; i++) {
      const a = (i * 2.399963) % 6.2832, r = 30 + ((i * 37) % 17), y = 4 + ((i * 53) % 26);
      starPositions.push(Math.cos(a) * r, y, Math.sin(a) * r);
    }
    const starGeometry = new THREE.BufferGeometry(); starGeometry.setAttribute("position", new THREE.Float32BufferAttribute(starPositions, 3));
    this.stars = new THREE.Points(starGeometry, new THREE.PointsMaterial({ color: 0xcdd6ff, size: 0.11, fog: false, transparent: true, opacity: 0.8 }));
    this.scene.add(this.stars);
    // Lamplighter itself: a small mint drone with its own light
    this.drone = new THREE.Group();
    this.droneCore = new THREE.Mesh(new THREE.OctahedronGeometry(0.2, 0), new THREE.MeshBasicMaterial({ toneMapped: false }));
    this.droneHalo = new THREE.Mesh(new THREE.TorusGeometry(0.34, 0.018, 8, 48), new THREE.MeshBasicMaterial({ toneMapped: false }));
    this.droneLight = new THREE.PointLight(0x52f0c0, 3, 5.5, 2);
    this.drone.add(this.droneCore, this.droneHalo, this.droneLight); this.drone.position.copy(HOME); this.scene.add(this.drone);
    this.flight = { from: HOME.clone(), to: HOME.clone(), start: -10, target: null };
    const geometry = new THREE.SphereGeometry(0.075, 10, 8);
    for (let i = 0; i < 170; i++) { const dot = new THREE.Mesh(geometry, m.ok); dot.visible = false; this.scene.add(dot); this.particles.push({ mesh: dot, edge: null, u: 0 }); }
  }

  setTheme(name) {
    const t = (this.theme = THEMES[name] || THEMES.dark), m = this.materials;
    this.scene.background = new THREE.Color(t.sky); this.scene.fog.color.set(t.sky);
    m.ground.color.set(t.ground); m.rim.color.set(t.rim); m.road.color.set(t.road); m.post.color.set(t.post);
    m.prop.color.set(t.prop); m.roof.color.set(t.roof); m.wire.color.set(t.wire);
    this.grid.material.color.set(t.grid); this.grid.material.vertexColors = false; this.grid.material.transparent = true;
    this.grid.material.opacity = name === "light" ? 0.9 : 0.55; this.grid.material.needsUpdate = true;
    this.hemi.color.set(t.hemiSky); this.hemi.groundColor.set(t.hemiGround); this.hemi.intensity = t.hemi;
    this.sun.color.set(t.sun); this.sun.intensity = t.sunPower;
    this.stars.visible = t.stars; this.bloom.strength = t.bloom; this.bloom.threshold = t.threshold;
    this.droneCore.material.color.set(t.mint); this.droneHalo.material.color.set(t.mint); this.droneLight.color.set(t.mint);
    m.ok.color.set(t.dot);
    this.renderer.toneMappingExposure = name === "light" ? 1.08 : 1.2;
  }

  // ---- topology ----
  update(topology, root) {
    this.root = root;
    const names = Object.keys(topology.services).sort();
    const signature = names.join("|") + "#" + topology.edges.map((e) => e.from + ">" + e.to).sort().join("|");
    if (signature !== this.signature) { this.signature = signature; this.rebuild(topology, names); }
    for (const name of names) {
      const node = this.nodes.get(name), data = topology.services[name];
      if (node.data && node.data.state !== "ok" && data.state === "ok") this.ring(name, LAMP);   // the lamp relights
      node.data = data;
    }
    for (const edge of this.edges) {
      const data = edge.from ? topology.edges.find((e) => e.from === edge.from && e.to === edge.to) : null;
      const entry = topology.services[edge.to];
      const calls = data ? data.calls : entry.requests, failed = data ? data.failed : entry.bad;
      edge.rate = calls / topology.span_s; edge.failShare = calls ? failed / calls : 0;
    }
  }

  rebuild(topology, names) {
    this.world.clear(); this.nodes.clear(); this.edges = []; this.tags.replaceChildren(); this.leaders.replaceChildren();
    const depth = new Map();
    const visit = (name, level) => {
      if ((depth.get(name) ?? -1) >= level || level > 12) return;
      depth.set(name, level); topology.services[name].deps.forEach((dep) => visit(dep, level + 1));
    };
    names.filter((n) => topology.services[n].edge).forEach((n) => visit(n, 0));
    names.forEach((n) => { if (!depth.has(n)) visit(n, 0); });
    const deepest = Math.max(0, ...depth.values()), columns = new Map();
    names.forEach((n) => columns.set(depth.get(n), (columns.get(depth.get(n)) || []).concat(n)));
    for (const [level, members] of columns) members.forEach((name, index) => {
      const position = new THREE.Vector3((level - deepest / 2) * 5.0, 0, (index - (members.length - 1) / 2) * 5.6);
      this.nodes.set(name, this.buildNode(name, position));
    });
    const m = this.materials;
    const link = (from, to, a, b) => {
      const mid = a.clone().lerp(b, 0.5); mid.y -= from ? 0.75 : -0.4;
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      this.world.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 40, 0.018, 6), m.wire));
      this.edges.push({ from, to, curve, rate: 0, failShare: 0, acc: 0 });
    };
    for (const e of topology.edges) {
      const a = this.nodes.get(e.from), b = this.nodes.get(e.to);
      if (!a || !b) continue;
      link(e.from, e.to, a.top.clone(), b.top.clone());
      const span = b.position.clone().sub(a.position), road = box(span.length(), 0.03, 0.7, m.road, 0, 0.016, 0, this.world, false);
      road.position.copy(a.position).lerp(b.position, 0.5).setY(0.016); road.rotation.y = -Math.atan2(span.z, span.x);
    }
    const entry = names.find((n) => topology.services[n].edge);
    if (entry) { const g = this.nodes.get(entry); link(null, entry, g.top.clone().add(new THREE.Vector3(-7.5, -0.6, 3.2)), g.top.clone()); }
  }

  buildNode(name, position) {
    const m = this.materials, group = new THREE.Group(); group.position.copy(position); this.world.add(group);
    const plinth = new THREE.Mesh(new THREE.CylinderGeometry(1.25, 1.38, 0.24, 48), m.roof); plinth.position.y = 0.12;
    plinth.receiveShadow = true; plinth.castShadow = true; group.add(plinth);
    const pool = new THREE.Mesh(new THREE.RingGeometry(0.98, 1.2, 64), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.9, toneMapped: false }));
    pool.rotation.x = -Math.PI / 2; pool.position.y = 0.25; group.add(pool);
    const post = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.075, LANTERN_Y - 0.5, 12), m.post); post.position.set(0.55, (LANTERN_Y - 0.5) / 2 + 0.24, 0.55);
    post.castShadow = true; group.add(post);
    const glass = new THREE.MeshStandardMaterial({ emissive: LAMP.clone(), emissiveIntensity: 2, color: 0x222222, roughness: 0.3 });
    const lantern = new THREE.Mesh(new THREE.BoxGeometry(0.36, 0.5, 0.36), glass); lantern.position.set(0.55, LANTERN_Y, 0.55); group.add(lantern);
    const cap = new THREE.Mesh(new THREE.ConeGeometry(0.34, 0.2, 4), m.post); cap.position.set(0.55, LANTERN_Y + 0.35, 0.55); cap.rotation.y = Math.PI / 4; group.add(cap);
    const light = new THREE.PointLight(LAMP, 4, 9, 1.8); light.position.set(0.55, LANTERN_Y - 0.1, 0.55); group.add(light);
    const windows = new THREE.MeshStandardMaterial({ emissive: LAMP.clone(), emissiveIntensity: 1.2, color: 0x111111 });
    this.buildProp(name, group, windows);
    const halo = new THREE.Mesh(new THREE.TorusGeometry(1.5, 0.035, 10, 72), new THREE.MeshBasicMaterial({ color: 0xff3d62, transparent: true, toneMapped: false }));
    halo.rotation.x = Math.PI / 2; halo.position.y = 0.3; halo.visible = false; group.add(halo);
    const tag = document.createElement("div"); tag.className = "tag";
    const label = document.createElement("b"), note = document.createElement("span"), flag = document.createElement("i");
    label.textContent = name; flag.textContent = "root cause"; tag.append(flag, label, note); this.tags.append(tag);
    const leader = document.createElementNS("http://www.w3.org/2000/svg", "path"); this.leaders.append(leader);
    return { name, group, position, leader, text: "", tagWidth: 0, tagX: null, top: position.clone().add(new THREE.Vector3(0.55, LANTERN_Y, 0.55)), glass, windows, light, pool, halo, tag, note, glow: 1, data: null };
  }

  buildProp(name, group, windows) {
    const m = this.materials, at = (w, h, d, material, x, y, z) => box(w, h, d, material, x, y, z, group);
    if (/gate|edge|proxy|ingress/.test(name)) {                    // an archway: where customers come in
      at(0.3, 1.5, 0.3, m.prop, -0.75, 0.99, -0.1); at(0.3, 1.5, 0.3, m.prop, 0.05, 0.99, -0.1); at(1.3, 0.28, 0.42, m.roof, -0.35, 1.88, -0.1);
      at(0.5, 0.07, 0.05, windows, -0.35, 1.88, 0.12);
    } else if (/check|cart|order|shop/.test(name)) {               // a kiosk with an awning
      at(1.15, 1.0, 0.95, m.prop, -0.3, 0.74, -0.25); const awning = at(1.3, 0.07, 0.6, m.roof, -0.3, 1.2, 0.35); awning.rotation.x = 0.35;
      at(0.8, 0.34, 0.05, windows, -0.3, 0.86, 0.24);
    } else if (/invent|stock|ware|catalog/.test(name)) {           // a stack of crates
      at(0.72, 0.72, 0.72, m.prop, -0.55, 0.6, -0.3); at(0.72, 0.72, 0.72, m.roof, 0.18, 0.6, -0.42); at(0.66, 0.66, 0.66, m.prop, -0.22, 1.29, -0.36);
      at(0.4, 0.07, 0.05, windows, -0.55, 0.66, 0.07); at(0.4, 0.07, 0.05, windows, -0.22, 1.32, -0.02);
    } else if (/pay|bank|bill|ledger/.test(name)) {                // a little bank
      at(1.35, 0.14, 0.95, m.roof, -0.3, 0.31, -0.25); for (const x of [-0.82, -0.3, 0.22]) at(0.16, 1.0, 0.16, m.prop, x, 0.88, 0.08);
      at(1.2, 0.95, 0.5, m.prop, -0.3, 0.86, -0.42); at(1.45, 0.2, 1.0, m.roof, -0.3, 1.48, -0.25); at(0.7, 0.07, 0.05, windows, -0.3, 1.48, 0.27);
    } else {                                                        // anything else: a small tower
      at(0.85, 1.3, 0.85, m.prop, -0.3, 0.89, -0.25); at(0.6, 0.5, 0.6, m.roof, -0.3, 1.79, -0.25);
      at(0.5, 0.07, 0.05, windows, -0.3, 1.2, 0.19); at(0.5, 0.07, 0.05, windows, -0.3, 0.9, 0.19);
    }
  }

  // ---- events ----
  ring(name, color) {
    const node = this.nodes.get(name); if (!node) return;
    const mesh = new THREE.Mesh(new THREE.TorusGeometry(1, 0.04, 8, 72), new THREE.MeshBasicMaterial({ color, transparent: true, toneMapped: false }));
    mesh.rotation.x = Math.PI / 2; mesh.position.copy(node.top); this.scene.add(mesh);
    this.rings.push({ mesh, born: this.clock.elapsedTime });
  }
  sendTo(name) {                       // Lamplighter flies to a lamp and stays with it
    if (this.flight.target === name || !this.nodes.has(name)) return;
    this.flight = { from: this.drone.position.clone(), to: this.nodes.get(name).top.clone().add(new THREE.Vector3(0, 0.95, 0)), start: this.clock.elapsedTime, target: name };
  }
  recall() {
    if (this.flight.target === null) return;
    this.flight = { from: this.drone.position.clone(), to: HOME.clone(), start: this.clock.elapsedTime, target: null };
  }
  mint() { return new THREE.Color(this.theme.mint); }

  // External labelling: every label sits in one band under the scene, in the same left-to-right order as
  // the lamps, never on top of the scene or of another label, joined to its lamp by a thin leader line.
  placeTags(width, height, dt) {
    const nodes = [...this.nodes.values()].filter((n) => n.anchor).sort((a, b) => a.anchor[0] - b.anchor[0]);
    if (!nodes.length) return;
    const gap = 12, margin = 10, bandTop = height - 70;
    for (const node of nodes) if (!node.tagWidth) node.tagWidth = node.tag.offsetWidth || 120;
    const total = nodes.reduce((sum, n) => sum + n.tagWidth, 0) + gap * (nodes.length - 1);
    const squeeze = Math.min(1, (width - 2 * margin) / total);     // only matters on very narrow screens
    let cursor = -Infinity;
    for (const node of nodes) {                                     // left to right: keep order, forbid overlap
      const w = node.tagWidth * squeeze;
      node.want = Math.max(node.anchor[0] - w / 2, cursor); cursor = node.want + w + gap;
    }
    let limit = width - margin;
    for (let i = nodes.length - 1; i >= 0; i--) {                    // right to left: pull back inside the view
      const w = nodes[i].tagWidth * squeeze;
      nodes[i].want = Math.max(margin, Math.min(nodes[i].want, limit - w)); limit = nodes[i].want - gap;
    }
    for (const node of nodes) {
      node.tagX = node.tagX === null ? node.want : node.tagX + (node.want - node.tagX) * Math.min(1, dt * 8);
      const w = node.tagWidth * squeeze, cx = node.tagX + w / 2, ay = Math.min(node.anchor[1], bandTop - 14);
      node.tag.style.transform = `translate(${node.tagX.toFixed(1)}px, ${bandTop}px) scale(${squeeze.toFixed(3)})`;
      const elbow = bandTop - 12;
      node.leader.setAttribute("d", `M${node.anchor[0].toFixed(1)} ${ay.toFixed(1)} L${node.anchor[0].toFixed(1)} ${elbow} L${cx.toFixed(1)} ${elbow} L${cx.toFixed(1)} ${bandTop}`);
      node.leader.setAttribute("class", node.tag.className.replace("tag", "lead"));
    }
  }

  // ---- frame ----
  frame() {
    const dt = Math.min(this.clock.getDelta(), 0.05), t = this.clock.elapsedTime, theme = this.theme;
    const width = this.canvas.clientWidth, height = this.canvas.clientHeight;
    if (this.width !== width || this.height !== height) {
      this.width = width; this.height = height; this.renderer.setSize(width, height, false); this.composer.setSize(width, height);
      this.camera.aspect = width / Math.max(height, 1); this.camera.updateProjectionMatrix();
    }
    // camera: slow drift, a little parallax, and it leans toward the root cause while an incident is open
    const rootNode = this.root ? this.nodes.get(this.root) : null;
    const wanted = rootNode ? rootNode.position.clone().multiplyScalar(0.3).setY(-1.2) : new THREE.Vector3(0, -1.2, 0);
    this.focus.lerp(wanted, Math.min(1, dt * 1.5));
    const angle = -0.62 + (this.calm ? 0 : Math.sin(t * 0.12) * 0.05) + this.pointer.x * 0.1;
    const radius = (rootNode ? 20 : 21.5) * Math.max(1, 1.55 / this.camera.aspect) * (this.camera.aspect > 2.4 ? 0.85 : 1), lift = 9.6 - this.pointer.y * 0.8;
    const goal = new THREE.Vector3(this.focus.x + Math.sin(angle) * radius, lift, this.focus.z + Math.cos(angle) * radius);
    if (this.camera.position.lengthSq() < 1) this.camera.position.copy(goal); else this.camera.position.lerp(goal, Math.min(1, dt * 2.2));
    this.camera.lookAt(this.focus);

    for (const node of this.nodes.values()) {
      const state = node.data ? node.data.state : "ok";
      const flicker = 0.45 + 0.55 * Math.abs(Math.sin(t * 9 + node.position.x) * Math.sin(t * 2.3 + node.position.z));
      const target = state === "ok" ? 1 : state === "failing" ? flicker : 0.03;
      node.glow += (target - node.glow) * Math.min(1, dt * 7);
      const color = state === "ok" ? LAMP : EMBER;
      node.glass.emissive.copy(state === "dark" ? OUT : color); node.glass.emissiveIntensity = theme.glass * (0.15 + node.glow);
      node.windows.emissive.copy(state === "dark" ? OUT : color); node.windows.emissiveIntensity = theme.glass * 0.5 * (0.1 + node.glow);
      node.light.color.copy(color); node.light.intensity = theme.lamp * node.glow;
      node.pool.material.color.copy(state === "dark" ? EMBER : color); node.pool.material.opacity = state === "dark" ? 0.35 + 0.35 * Math.sin(t * 5) : 0.25 + 0.6 * node.glow;
      const isRoot = this.root === node.name && state !== "ok";
      node.halo.visible = isRoot;
      if (isRoot) { const pulse = (t * 0.8) % 1; node.halo.scale.setScalar(0.85 + pulse * 0.55); node.halo.material.opacity = 1 - pulse; }
      // remember where the plinth is on screen; labels are laid out together below
      const screen = node.position.clone().add(new THREE.Vector3(0, 0.1, 1.5)).project(this.camera);
      node.anchor = [((screen.x + 1) / 2) * width, ((1 - screen.y) / 2) * height];
      const cls = "tag " + state + (isRoot ? " root" : "");
      if (node.tag.className !== cls) { node.tag.className = cls; node.tagWidth = 0; }
      if (node.data) {
        const share = node.data.requests ? Math.round((100 * node.data.bad) / node.data.requests) : 0;
        const text = state === "dark" ? "not answering" : Math.round(node.data.per_s) + " req/s" + (share ? ", " + share + "% failing" : "");
        if (node.text !== text) { node.text = text; node.note.textContent = text; node.tagWidth = 0; }
      }
    }
    this.placeTags(width, height, dt);
    // requests on the wires; a failed one burns out before it arrives
    if (!this.calm) for (const edge of this.edges) {
      edge.acc += Math.min(edge.rate, 14) * dt;
      while (edge.acc >= 1) {
        edge.acc -= 1; const free = this.particles.find((p) => !p.edge); if (!free) break;
        free.edge = edge; free.u = 0; free.failed = Math.random() < edge.failShare; free.speed = 0.5 + Math.random() * 0.2;
        free.mesh.material = free.failed ? this.materials.bad : this.materials.ok; free.mesh.visible = true;
      }
    }
    for (const p of this.particles) {
      if (!p.edge) continue;
      p.u += p.speed * dt; const limit = p.failed ? 0.78 : 1;
      if (p.u >= limit || !this.edges.includes(p.edge)) { p.edge = null; p.mesh.visible = false; continue; }
      p.edge.curve.getPoint(p.u, p.mesh.position); p.mesh.scale.setScalar(p.failed ? 1.25 * (1 - p.u / limit) + 0.1 : 1);
    }
    // Lamplighter
    const k = Math.min(1, (t - this.flight.start) / 1.7), ease = k * k * (3 - 2 * k);
    this.drone.position.lerpVectors(this.flight.from, this.flight.to, ease);
    this.drone.position.y += Math.sin(Math.PI * ease) * 1.5 + (this.calm ? 0 : Math.sin(t * 2.2) * 0.07);
    this.droneCore.rotation.y = t * 1.6; this.droneHalo.rotation.set(Math.PI / 2 + Math.sin(t * 1.3) * 0.4, t * 0.9, 0);
    this.droneLight.intensity = (this.flight.target ? 5 : 2.2) * (theme === THEMES.light ? 0.5 : 1);
    this.rings = this.rings.filter((ring) => {
      const u = (t - ring.born) / 1.5;
      if (u >= 1) { this.scene.remove(ring.mesh); ring.mesh.geometry.dispose(); ring.mesh.material.dispose(); return false; }
      ring.mesh.scale.setScalar(0.4 + u * 3.4); ring.mesh.material.opacity = 1 - u; return true;
    });
    if (!this.calm) this.stars.rotation.y = t * 0.004;
    this.composer.render();
  }
}
