import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'

const byId = id => document.getElementById(id)
const status = byId('status')
const format = ts => ts.slice(0, 19).replace('T', ' ')

async function loadMeasurements() {
  const response = await fetch('./measurements.json')
  if (!response.ok) throw new Error(`계측값을 불러올 수 없습니다 (${response.status}).`)
  return response.json()
}

function replay({ frames, tags, plant_id, unit_id, equip_id, outlet_node }) {
  byId('unit').textContent = `${plant_id} / ${unit_id}`
  byId('route').textContent = `${equip_id} → ${outlet_node}`
  const timeline = byId('timeline')
  const play = byId('play')
  timeline.max = frames.length - 1
  timeline.disabled = play.disabled = false
  const cells = tags.map(({ tag_id }) => {
    const row = byId('readings').insertRow()
    const label = document.createElement('th')
    label.scope = 'row'
    label.textContent = tag_id
    row.append(label)
    return row.insertCell()
  })

  function renderFrame() {
    const { at, samples } = frames[Number(timeline.value)]
    const values = new Map(samples.map(sample => [sample.tag_id, sample.value]))
    tags.forEach(({ tag_id, unit }, i) => {
      cells[i].textContent = values.has(tag_id) ? `${values.get(tag_id)} ${unit}` : '미수신'
    })
    byId('at').textContent = format(at)
    byId('sample-time').textContent = samples.length ? `측정 시각 (KST) ${format(samples[0].ts)}` : ''
  }

  let timer
  function pause() {
    clearInterval(timer)
    timer = undefined
    play.textContent = '재생'
  }
  timeline.addEventListener('input', renderFrame)
  play.addEventListener('click', () => {
    if (timer) return pause()
    if (timeline.value === timeline.max) timeline.value = 0
    renderFrame()
    play.textContent = '일시 정지'
    timer = setInterval(() => {
      timeline.stepUp()
      renderFrame()
      if (timeline.value === timeline.max) pause()
    }, 1000)
  })
  renderFrame()
}

async function loadModel() {
  const { scene: model } = await new GLTFLoader().loadAsync('./cdu.glb')
  const container = byId('model')
  const renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5))
  container.append(renderer.domElement)
  const scene = new THREE.Scene()
  scene.background = new THREE.Color(0x112c3a)
  scene.add(model, new THREE.HemisphereLight(0xf1fcff, 0x8295a1, 2))
  const key = new THREE.DirectionalLight(0xffffff, 3)
  key.position.set(-3, 7, 5)
  scene.add(key)
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100)
  const bounds = new THREE.Box3().setFromObject(model)
  const center = bounds.getCenter(new THREE.Vector3())
  const size = bounds.getSize(new THREE.Vector3())
  const controls = new OrbitControls(camera, renderer.domElement)
  controls.target.copy(center)
  const render = () => renderer.render(scene, camera)
  controls.addEventListener('change', render)
  new ResizeObserver(() => {
    const { clientWidth: width, clientHeight: height } = container
    camera.aspect = width / height
    const halfFov = THREE.MathUtils.degToRad(camera.fov / 2)
    const frameDistance = Math.max(size.x / camera.aspect, size.y) / (2 * Math.tan(halfFov))
    const distance = frameDistance * 1.16 + size.x * 0.19
    camera.position.copy(center).add(new THREE.Vector3(0.38, 0.37, 0.85).multiplyScalar(distance))
    camera.updateProjectionMatrix()
    renderer.setSize(width, height, false)
    controls.update()
    render()
  }).observe(container)
}

const results = await Promise.allSettled([loadMeasurements().then(replay), loadModel()])
const errors = ['계측 자료', '3D 모형'].filter((_, i) => results[i].status === 'rejected')
status.textContent = errors.length ? `${errors.join(' 및 ')}을 불러올 수 없습니다.` : ''
