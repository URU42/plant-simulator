import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'

test('기존 설비 형상을 GLB로 내보내고 웹 루트에 계측값만 배치한다', () => {
  const temp = mkdtempSync(join(tmpdir(), 'cdu-demo-test-'))
  try {
    const output = join(temp, 'run')
    const frontend = process.env.CDU_DEMO_FRONTEND ?? resolve('../sk-clx-diag-agent/apps/frontend')
    execFileSync(process.env.PYTHON ?? 'python', ['-m', 'simulator.physics_demo', output])
    execFileSync(process.execPath, ['demo/cdu/build.mjs', frontend, output])
    const publicDir = join(output, 'public')
    assert.deepEqual(readdirSync(publicDir).sort(), ['assets', 'cdu.glb', 'index.html', 'measurements.json'])
    const bytes = readFileSync(join(publicDir, 'cdu.glb'))
    assert.equal(bytes.toString('ascii', 0, 4), 'glTF')
    assert.equal(bytes.readUInt32LE(4), 2)
    assert.equal(bytes.readUInt32LE(8), bytes.length)
    const jsonSize = bytes.readUInt32LE(12)
    const gltf = JSON.parse(bytes.toString('utf8', 20, 20 + jsonSize))
    const names = gltf.nodes.map(node => node.name)
    for (const name of ['P-8101A', 'P-8101A → DS', 'DS']) assert.ok(names.includes(name), name)
    assert.ok(gltf.meshes.length > 3)
    assert.ok(gltf.buffers.every(buffer => buffer.uri === undefined))
    const publicText = readFileSync(join(publicDir, 'measurements.json'), 'utf8')
    assert.doesNotMatch(publicText, /CDU-DEMO-|PM-SUCTION-LOSS|mass_residual|energy_residual/)
    assert.ok(readFileSync(join(output, 'private', 'truth.json'), 'utf8').includes('PM-SUCTION-LOSS'))
  } finally {
    rmSync(temp, { recursive: true })
  }
})
