import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Search } from 'lucide-react'

/**
 * Выпадающий список вместо системного <select>.
 *
 * options: [{ value, label, hint?, disabled? }]; value сравнивается как строка, поэтому числа и '' работают одинаково.
 * onChange(value) получает исходное значение из options, а не событие.
 * Меню рисуется в портале с fixed-позицией: его не обрезают таблицы с прокруткой и модальные окна.
 * Клавиатура: стрелки, Home/End, Enter, Escape, Tab; в длинных списках есть поиск.
 */
export default function Select({
  value, onChange, options, placeholder = 'Выберите', ariaLabel, disabled = false,
  searchable, size = 'md', width, className = '',
}) {
  const id = useId()
  const btnRef = useRef(null)
  const menuRef = useRef(null)
  const listRef = useRef(null)
  const searchRef = useRef(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(-1)
  const [pos, setPos] = useState(null)

  const withSearch = searchable ?? options.length > 8
  const current = options.find((o) => String(o.value) === String(value ?? ''))
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? options.filter((o) => `${o.label} ${o.hint || ''}`.toLowerCase().includes(q)) : options
  }, [options, query])

  const place = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect()
    if (!r) return
    const maxH = 320
    const below = window.innerHeight - r.bottom - 8
    const up = below < Math.min(maxH, 200) && r.top > below
    // ширина - по самому длинному пункту, но не уже кнопки и не шире экрана; у правого края сдвигаем влево
    const w = menuRef.current ? menuRef.current.getBoundingClientRect().width : Math.max(r.width, 200)
    setPos({
      left: Math.max(8, Math.min(r.left, window.innerWidth - w - 8)),
      minWidth: Math.max(r.width, 180),
      maxHeight: Math.max(160, Math.min(maxH, (up ? r.top : below) - 8)),
      ...(up ? { bottom: window.innerHeight - r.top + 4 } : { top: r.bottom + 4 }),
    })
  }, [])

  const openMenu = () => {
    if (disabled) return
    setQuery('')
    const i = options.findIndex((o) => String(o.value) === String(value ?? ''))
    setActive(i >= 0 ? i : options.findIndex((o) => !o.disabled))
    place()
    setOpen(true)
  }
  const close = (refocus = true) => {
    setOpen(false)
    if (refocus) btnRef.current?.focus()
  }
  const pick = (o) => {
    if (!o || o.disabled) return
    close()
    if (String(o.value) !== String(value ?? '')) onChange(o.value)
  }

  // меню следует за кнопкой при прокрутке и изменении размера окна; клик вне - закрывает
  useLayoutEffect(() => {
    if (!open) return undefined
    const onOutside = (e) => {
      if (!btnRef.current?.contains(e.target) && !menuRef.current?.contains(e.target)) close(false)
    }
    const onScroll = (e) => { if (!menuRef.current?.contains(e.target)) place() }
    document.addEventListener('mousedown', onOutside)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', place)
    return () => {
      document.removeEventListener('mousedown', onOutside)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', place)
    }
  }, [open, place])

  // после первой отрисовки меню знаем его настоящую ширину - уточняем положение, чтобы не вылезло за край
  useLayoutEffect(() => { if (open) place() }, [open, place])
  useEffect(() => { if (open && withSearch) searchRef.current?.focus() }, [open, withSearch])
  useEffect(() => { if (open) setActive(shown.findIndex((o) => !o.disabled)) }, [query]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (open && active >= 0) listRef.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [open, active])

  const move = (step) => {
    if (!shown.length) return
    let i = active
    for (let n = 0; n < shown.length; n += 1) {
      i = (i + step + shown.length) % shown.length
      if (!shown[i].disabled) break
    }
    setActive(i)
  }
  const onKey = (e) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); openMenu() }
      return
    }
    // Escape не должен закрывать модальное окно, в котором стоит список
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close() } else if (e.key === 'ArrowDown') { e.preventDefault(); move(1) } else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) } else if (e.key === 'Home') { e.preventDefault(); setActive(shown.findIndex((o) => !o.disabled)) } else if (e.key === 'End') { e.preventDefault(); setActive(shown.length - 1 - [...shown].reverse().findIndex((o) => !o.disabled)) } else if (e.key === 'Enter' || (e.key === ' ' && !withSearch)) { e.preventDefault(); pick(shown[active]) } else if (e.key === 'Tab') close(false)
  }

  return (
    <>
      <button
        ref={btnRef} type="button" id={id}
        className={`sel sel-${size} ${open ? 'open' : ''} ${current ? '' : 'empty'} ${className}`}
        style={width ? { width } : undefined}
        onClick={() => (open ? close() : openMenu())} onKeyDown={onKey} disabled={disabled}
        aria-haspopup="listbox" aria-expanded={open} aria-label={ariaLabel}
        aria-controls={open ? `${id}-list` : undefined}
      >
        <span className="sel-value">{current ? current.label : placeholder}</span>
        <ChevronDown size={15} className="sel-chevron" aria-hidden />
      </button>
      {open && pos && createPortal(
        <div ref={menuRef} className="sel-menu" style={{ position: 'fixed', left: pos.left, minWidth: pos.minWidth, top: pos.top, bottom: pos.bottom, maxHeight: pos.maxHeight }}>
          {withSearch && (
            <div className="sel-search">
              <Search size={14} aria-hidden />
              <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey}
                placeholder="Найти" aria-label="Поиск по списку" aria-controls={`${id}-list`}
                aria-activedescendant={active >= 0 ? `${id}-o${active}` : undefined} />
            </div>
          )}
          <ul ref={listRef} id={`${id}-list`} role="listbox" aria-label={ariaLabel} className="sel-list">
            {shown.length === 0 && <li className="sel-none">Ничего не найдено</li>}
            {shown.map((o, i) => {
              const selected = String(o.value) === String(value ?? '')
              return (
                <li key={`${o.value}`} id={`${id}-o${i}`} data-i={i} role="option" aria-selected={selected} aria-disabled={o.disabled || undefined}
                  className={`sel-opt ${i === active ? 'active' : ''} ${selected ? 'selected' : ''} ${o.disabled ? 'disabled' : ''}`}
                  onMouseEnter={() => !o.disabled && setActive(i)}
                  onMouseDown={(e) => e.preventDefault()} onClick={() => pick(o)}>
                  <span className="sel-opt-text">
                    <span>{o.label}</span>
                    {o.hint && <span className="sel-hint">{o.hint}</span>}
                  </span>
                  {selected && <Check size={15} className="sel-check" aria-hidden />}
                </li>
              )
            })}
          </ul>
        </div>,
        document.body,
      )}
    </>
  )
}

/** Быстрые преобразования справочников в options. */
export const fromMap = (map) => Object.entries(map).map(([value, label]) => ({ value, label }))
export const fromList = (list, label = (x) => x.name, hint) => list.map((x) => ({ value: x.id, label: label(x), hint: hint ? hint(x) : undefined }))
