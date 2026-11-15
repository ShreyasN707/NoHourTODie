import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ModuleSwitcher } from './advisor/ModuleSwitcher.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ModuleSwitcher fleet={<App />} />
  </StrictMode>,
)
