/* Icons: one stroke family (24 grid, 1.8 stroke), drawn here so nothing depends on an icon font. */
const ICONS: Record<string, string> = {
  download: 'M12 4v11m0 0-5-5m5 5 5-5M5 20h14',
  graph: 'M4 6h6v5H4zM14 13h6v5h-6zM10 8.5h2.5a2 2 0 0 1 2 2V13',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6',
  close: 'M6 6l12 12M18 6 6 18',
  star: 'M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z',
  trash: 'M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13M10 11v5M14 11v5',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  pencil: 'M4 20h4L19 9l-4-4L4 16zM13 7l4 4',
  receipt: 'M7 3h10v18l-2.5-1.6L12 21l-2.5-1.6L7 21zM10 8h4M10 12h4',
  upload: 'M12 16V5m0 0-4.5 4.5M12 5l4.5 4.5M5 19h14',
  github: 'M9 19c-4 1.3-4-2-6-2.5m12 4.5v-3.4a3 3 0 0 0-.8-2.3c2.7-.3 5.6-1.3 5.6-6a4.6 4.6 0 0 0-1.3-3.2 4.3 4.3 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.7 11.7 0 0 0-6 0C6.6 2.6 5.6 2.9 5.6 2.9a4.3 4.3 0 0 0-.1 3.2A4.6 4.6 0 0 0 4.2 9.3c0 4.6 2.9 5.7 5.6 6a3 3 0 0 0-.8 2.3V21',
  play: 'M8 5.5v13l11-6.5z',
  pause: 'M8 5v14M16 5v14',
  sound: 'M4 9.5h3.5L12 5v14l-4.5-4.5H4zM16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11',
  muted: 'M4 9.5h3.5L12 5v14l-4.5-4.5H4zM16.5 9.5l5 5M21.5 9.5l-5 5',
  expand: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  up: 'M12 19V5m0 0-6 6m6-6 6 6',
  left: 'M19 12H5m0 0 6-6m-6 6 6 6',
  right: 'M5 12h14m0 0-6-6m6 6-6 6',
  down: 'M12 5v14m0 0-6-6m6 6 6-6',
  plus: 'M12 5v14M5 12h14',
  orbit: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM4 12c0-2.5 3.6-4.5 8-4.5s8 2 8 4.5-3.6 4.5-8 4.5c-1.3 0-2.6-.2-3.7-.5M7 18.5 8.3 16 5.6 15.3',
  cut: 'M8.1 8.1 20 20M8.1 15.9 20 4M6 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM6 15.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z',
  dice: 'M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01',
}

export function Icon({ name }: { name: keyof typeof ICONS }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  )
}
