import { useMemo, useRef } from 'react'

type Props = {
  value: string
  onChange: (value: string) => void
  readOnly?: boolean
  label?: string
  placeholder?: string
}

const tokenPattern = /(#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|False|finally|for|from|global|if|import|in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield)\b|\b(?:print|len|range|str|int|float|list|dict|set|tuple|sum|min|max|enumerate|zip|super|self)\b|\b\d+(?:\.\d+)?\b)/g

function highlight(line: string, lineIndex: number) {
  const fragments = line.split(tokenPattern)
  return fragments.map((fragment, index) => {
    if (!fragment) return null
    let kind = ''
    if (fragment.startsWith('#')) kind = 'tok-comment'
    else if (/^["']/.test(fragment)) kind = 'tok-string'
    else if (/^\d/.test(fragment)) kind = 'tok-number'
    else if (/^(?:print|len|range|str|int|float|list|dict|set|tuple|sum|min|max|enumerate|zip|super|self)$/.test(fragment)) kind = 'tok-builtin'
    else if (tokenPattern.test(fragment)) kind = 'tok-keyword'
    tokenPattern.lastIndex = 0
    return kind
      ? <span className={kind} key={`${lineIndex}-${index}`}>{fragment}</span>
      : <span key={`${lineIndex}-${index}`}>{fragment}</span>
  })
}

export function PythonEditor({ value, onChange, readOnly = false, label = 'Python source', placeholder }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const highlighted = useMemo(() => value.split('\n').map((line, i) => (
    <span className="editor-line" key={i}>{highlight(line, i)}{'\n'}</span>
  )), [value])

  return (
    <div className={`python-editor ${readOnly ? 'is-readonly' : ''}`}>
      <pre className="syntax-layer" aria-hidden="true">{highlighted}</pre>
      <textarea
        ref={textareaRef}
        aria-label={label}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        readOnly={readOnly}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        onScroll={(event) => {
          const layer = event.currentTarget.parentElement?.querySelector('.syntax-layer') as HTMLElement | null
          if (layer) {
            layer.scrollTop = event.currentTarget.scrollTop
            layer.scrollLeft = event.currentTarget.scrollLeft
          }
        }}
      />
    </div>
  )
}
