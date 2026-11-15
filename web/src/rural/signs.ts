// Map signs drawn after the Indian road-sign code (IRC:67) and ISO 7010, as small inline SVGs.
// Warning = white triangle, red border. Prohibitory = white disc, red ring. Informatory = blue board.
// Work-zone diversion = yellow board (IRC:SP:55). Assembly / meeting point = ISO 7010 E007 (green).

const tri = (inner: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 58"><path d="M32 3 61 54H3Z" fill="#fff" stroke="#d71920" stroke-width="6" stroke-linejoin="round"/>${inner}</svg>`

export const SIGNS = {
  // IRC "Falling rocks": rocks tumbling off a slope
  landslide: tri(
    `<path d="M14 48 30 20 40 48Z" fill="#111"/><circle cx="41" cy="26" r="4" fill="#111"/><circle cx="46" cy="35" r="3.2" fill="#111"/><circle cx="44" cy="44" r="2.6" fill="#111"/>`,
  ),
  // IRC "Hairpin bend"
  hairpin: tri(
    `<path d="M24 50V30a8 8 0 0 1 16 0v8" fill="none" stroke="#111" stroke-width="5"/><path d="M33 36h14l-7 9Z" fill="#111"/>`,
  ),
  // IRC "Slippery road": car with skid marks (shown on ghats in the monsoon)
  slippery: tri(
    `<rect x="22" y="24" width="20" height="10" rx="3" fill="#111"/><path d="M20 40c4 4 8-4 12 0s8-4 12 0M18 47c4 4 8-4 12 0s8-4 12 0" fill="none" stroke="#111" stroke-width="2.6"/>`,
  ),
  // IRC "Vehicles prohibited in both directions" = road closed
  closed: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="27" fill="#fff" stroke="#d71920" stroke-width="9"/><rect x="18" y="28" width="28" height="8" fill="#111"/></svg>`,
  // Work-zone diversion board (yellow, black arrow)
  diversion: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 56"><rect x="2" y="2" width="76" height="52" rx="5" fill="#ffd200" stroke="#111" stroke-width="3"/><text x="40" y="21" text-anchor="middle" font-family="Arial" font-weight="700" font-size="13" fill="#111">DIVERSION</text><path d="M14 38h40v-8l14 12-14 12v-8H14Z" fill="#111"/></svg>`,
  // IRC informatory "Hospital": blue board, white square, red cross
  hospital: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><rect x="2" y="2" width="52" height="52" rx="5" fill="#1d4f91" stroke="#fff" stroke-width="3"/><rect x="11" y="11" width="34" height="34" fill="#fff"/><path d="M24 15h8v9h9v8h-9v9h-8v-9h-9v-8h9Z" fill="#d71920"/></svg>`,
  // Same board with "H" for a smaller (taluk) hospital that can stabilise
  firstAid: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><rect x="2" y="2" width="52" height="52" rx="5" fill="#1d4f91" stroke="#fff" stroke-width="3"/><text x="28" y="40" text-anchor="middle" font-family="Arial" font-weight="700" font-size="32" fill="#fff">H</text></svg>`,
  // Hospital that turned the patient away
  refused: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><rect x="2" y="2" width="52" height="52" rx="5" fill="#1d4f91" stroke="#fff" stroke-width="3"/><rect x="11" y="11" width="34" height="34" fill="#fff"/><path d="M24 15h8v9h9v8h-9v9h-8v-9h-9v-8h9Z" fill="#d71920" opacity=".35"/><path d="M8 8 48 48M48 8 8 48" stroke="#d71920" stroke-width="6"/></svg>`,
  // ISO 7010 E007 assembly point, used as the crew-meets-patient point
  meet: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><rect x="2" y="2" width="52" height="52" rx="4" fill="#00843d" stroke="#fff" stroke-width="3"/><circle cx="24" cy="25" r="3.4" fill="#fff"/><circle cx="32" cy="25" r="3.4" fill="#fff"/><path d="M21 30h6v9h-6zM29 30h6v9h-6z" fill="#fff"/><path d="M8 8l8 8M48 8l-8 8M8 48l8-8M48 48l-8-8" stroke="#fff" stroke-width="3.5"/><path d="M14 16h3v-3M42 16h-3v-3M14 40h3v3M42 40h-3v3" fill="none" stroke="#fff" stroke-width="3"/></svg>`,
  // Mobile dead zone (not a road sign; grey badge so it reads as information, not a rule)
  noSignal: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><circle cx="28" cy="28" r="25" fill="#475569" stroke="#fff" stroke-width="3"/><path d="M15 38h4v-4h-4zM22 38h4v-9h-4zM29 38h4v-14h-4zM36 38h4v-19h-4z" fill="#fff"/><path d="M12 12 44 44" stroke="#ef4444" stroke-width="5"/></svg>`,
  patient: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><circle cx="28" cy="28" r="24" fill="#fff" stroke="#d71920" stroke-width="5"/><circle cx="28" cy="28" r="11" fill="#d71920"/></svg>`,
  ambulanceAls: amb('#dc2626'),
  ambulanceBls: amb('#2563eb'),
  ambulanceNeighbour: amb('#7c3aed'),
} as const

function amb(color: string) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 44"><rect x="2" y="2" width="60" height="40" rx="8" fill="${color}" stroke="#fff" stroke-width="3"/><path d="M8 30V14h28v16ZM36 18h9l7 7v5H36Z" fill="#fff"/><path d="M19 16h4v4h4v4h-4v4h-4v-4h-4v-4h4Z" fill="${color}"/><circle cx="17" cy="33" r="4" fill="#111" stroke="#fff" stroke-width="2"/><circle cx="45" cy="33" r="4" fill="#111" stroke="#fff" stroke-width="2"/></svg>`
}

export type SignId = keyof typeof SIGNS

export const signUrl = (id: SignId) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(SIGNS[id])}`

const SIZE: Partial<Record<SignId, [number, number]>> = {
  diversion: [80, 56],
  ambulanceAls: [64, 44],
  ambulanceBls: [64, 44],
  ambulanceNeighbour: [64, 44],
  landslide: [64, 58],
  hairpin: [64, 58],
  slippery: [64, 58],
}

/** deck.gl IconLayer icon definition (auto-packed from the data URL). */
export const icon = (id: SignId) => {
  const [w, h] = SIZE[id] ?? [64, 64]
  // explicit pixel size on the <svg> so the browser rasterises it sharply (default would be 300x150)
  const svg = SIGNS[id].replace('<svg ', `<svg width="${w * 2}" height="${h * 2}" `)
  return { id, url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`, width: w * 2, height: h * 2, anchorY: h }
}
