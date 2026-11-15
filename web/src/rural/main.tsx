import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import RuralView from './RuralView'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RuralView />
  </StrictMode>,
)
