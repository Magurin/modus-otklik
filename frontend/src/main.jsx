import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import '@fontsource-variable/inter'
import App from './App.jsx'
import { initTelegram } from './tg.js'
import './styles.css'

// внутри Telegram сначала получаем данные запуска Mini App, чтобы войти без пароля
initTelegram().finally(() => {
  createRoot(document.getElementById('root')).render(
    <React.StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </React.StrictMode>,
  )
})
