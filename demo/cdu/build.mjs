import { readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const [frontendPath, outputPath] = process.argv.slice(2)
if (!frontendPath || !outputPath) {
  throw new Error('사용법: node demo/cdu/build.mjs <진단 저장소의 apps/frontend> <재생 출력 디렉터리>')
}

const frontend = resolve(frontendPath)
const output = resolve(outputPath, 'public')
const data = JSON.parse(await readFile(resolve(output, 'measurements.json'), 'utf8'))
const dependency = createRequire(resolve(frontend, 'package.json'))
const importFrom = path => import(pathToFileURL(path).href)
const threeRoot = dirname(dependency.resolve('three'))
const addons = resolve(threeRoot, '../examples/jsm')
const THREE = await importFrom(resolve(threeRoot, 'three.module.js'))
const { GLTFExporter } = await importFrom(resolve(addons, 'exporters/GLTFExporter.js'))
const { buildEquipment, createMaterials, bake, roundedPath } = await importFrom(
  resolve(frontend, 'src/workspace/plant3d/equipment.js'),
)
const { build } = await importFrom(resolve(frontend, 'node_modules/vite/dist/node/index.js'))

// 텍스처 없는 GLB를 내보낼 때 Three.js가 사용하는 브라우저 FileReader를 Node에서 연결한다.
globalThis.FileReader ??= class {
  async readAsArrayBuffer(blob) {
    this.result = await blob.arrayBuffer()
    this.onloadend()
  }
}

const materials = createMaterials()
const { group, meta } = buildEquipment(materials, 'PM', { key: data.equip_id })
const pump = bake(group, { shadow: false })
pump.name = data.equip_id

// 기존 형상의 토출 노즐과 공정 경계점을 잇는 가상 배치.
const nozzle = meta.ports('out', 0, 1).p
const points = [
  nozzle, new THREE.Vector3(nozzle.x, 2.65, 0),
  new THREE.Vector3(4.3, 2.65, 0), new THREE.Vector3(4.3, 1.25, 0),
]
const path = roundedPath(points, 0.3)
const pipe = new THREE.Mesh(new THREE.TubeGeometry(path, 64, 0.125, 12, false), materials.steel)
pipe.name = `${data.equip_id} → ${data.outlet_node}`
const boundary = new THREE.Mesh(new THREE.BoxGeometry(0.65, 1.25, 0.65), materials.navy)
boundary.position.copy(points.at(-1)).y -= 0.625
boundary.name = data.outlet_node
const scene = new THREE.Scene()
scene.add(pump, pipe, boundary)
const glb = await new GLTFExporter().parseAsync(scene, { binary: true })
await writeFile(resolve(output, 'cdu.glb'), Buffer.from(glb))
await build({
  root: dirname(fileURLToPath(import.meta.url)),
  configFile: false,
  publicDir: false,
  base: './',
  resolve: { alias: [
    { find: /^three$/, replacement: resolve(threeRoot, 'three.module.js') },
    { find: /^three\/addons\//, replacement: addons + '/' },
  ] },
  build: { outDir: output, emptyOutDir: false, target: 'es2022' },
})
