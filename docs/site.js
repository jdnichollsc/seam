// Seam site: the hero graph (wires measured from the layout, effects cycling through real takes) and light scroll motion.
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  const graph = document.getElementById('graph')
  const svg = document.getElementById('wires')
  const NS = 'http://www.w3.org/2000/svg'
  const links = [['.n-a', '.n-fx'], ['.n-b', '.n-fx'], ['.n-fx', '.n-out']]

  // One wire per link plus a pulse riding on it; sockets at both ends of the node heads.
  const made = links.map(() => {
    const line = document.createElementNS(NS, 'path')
    const pulse = document.createElementNS(NS, 'path')
    pulse.setAttribute('class', 'pulse')
    const s1 = document.createElementNS(NS, 'circle')
    const s2 = document.createElementNS(NS, 'circle')
    s1.setAttribute('r', '5')
    s2.setAttribute('r', '5')
    svg.append(line, pulse, s1, s2)
    return { line, pulse, s1, s2 }
  })

  function draw() {
    const g = graph.getBoundingClientRect()
    const stacked = innerWidth <= 900
    links.forEach(([from, to], i) => {
      const a = graph.querySelector(from).getBoundingClientRect()
      const b = graph.querySelector(to).getBoundingClientRect()
      const head = 22  // sockets sit on the node heads, like ComfyUI
      let p, q, d
      if (stacked) {
        p = { x: a.left + a.width / 2 - g.left, y: a.bottom - g.top }
        q = { x: b.left + b.width / 2 - g.left, y: b.top - g.top }
        const dy = Math.max(20, (q.y - p.y) / 2)
        d = `M${p.x} ${p.y}C${p.x} ${p.y + dy} ${q.x} ${q.y - dy} ${q.x} ${q.y}`
      } else {
        p = { x: a.right - g.left, y: a.top + head - g.top }
        q = { x: b.left - g.left, y: b.top + head - g.top }
        const dx = Math.max(30, (q.x - p.x) / 2)
        d = `M${p.x} ${p.y}C${p.x + dx} ${p.y} ${q.x - dx} ${q.y} ${q.x} ${q.y}`
      }
      const m = made[i]
      m.line.setAttribute('d', d)
      m.pulse.setAttribute('d', d)
      m.s1.setAttribute('cx', p.x); m.s1.setAttribute('cy', p.y)
      m.s2.setAttribute('cx', q.x); m.s2.setAttribute('cy', q.y)
    })
  }
  draw()
  new ResizeObserver(draw).observe(graph)
  addEventListener('resize', draw)

  // The effect node cycles through the three qualified effects; the output plays that real take, then the next.
  const out = document.getElementById('g-out')
  const items = [...document.querySelectorAll('#fx-list li')]
  let i = 0
  const show = (n) => {
    i = n % items.length
    items.forEach((li, k) => li.classList.toggle('on', k === i))
    const fx = items[i].dataset.fx
    out.poster = `media/site/p2_${fx}.jpg`
    out.src = `media/site/p2_${fx}.mp4`
    out.play().catch(() => {})
    if (!reduce && window.gsap) {
      gsap.fromTo(out, { clipPath: 'inset(0% 50% 0% 50%)' }, { clipPath: 'inset(0% 0% 0% 0%)', duration: 1, ease: 'expo.out' })
    }
  }
  out.addEventListener('ended', () => show(i + 1))
  items.forEach((li, k) => {
    li.tabIndex = 0
    li.setAttribute('role', 'button')
    li.addEventListener('click', () => show(k))
    li.addEventListener('keydown', (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), show(k)))
  })

  // Reel: each take plays while it is on screen.
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const v = e.target
      if (e.isIntersecting) v.play().catch(() => {})
      else v.pause()
    }
  }, { threshold: 0.4 })
  document.querySelectorAll('.take video, .showcase video').forEach((v) => io.observe(v))

  if (reduce || !window.gsap) return

  // Signature: pulses flow along the wires into the output, like Seam while it renders.
  made.forEach((m, k) => gsap.to(m.pulse, { strokeDashoffset: -48, duration: 1.1, ease: 'none', repeat: -1, delay: k * 0.2 }))

  // The headline tags land; the graph is already on screen (nothing waits on scroll to become visible).
  gsap.from('.hero-title .tag', { yPercent: 40, opacity: 0, skewX: -24, duration: 0.9, stagger: 0.12, ease: 'expo.out' })
})()
