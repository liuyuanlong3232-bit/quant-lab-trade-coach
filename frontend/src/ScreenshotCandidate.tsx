import { useEffect, useMemo, useState } from 'react'
import { postCoach } from './data'

export type ScreenshotKind = 'holding' | 'trade'
export type AccountCandidate = { shares: string; avg_cost: string; available_cash: string; total_assets: string }
type TradeCandidate = { side: string; quantity: string; price: string; fees: string; time: string; reason: string }
export type FieldEvidence = { confidence: number | null; label: string; excerpt: string }
export type ParsedScreenshot = { account: AccountCandidate; trade: TradeCandidate; evidence: Record<string, FieldEvidence>; warnings: string[] }

export function emptyAccountCandidate(): AccountCandidate { return { shares: '', avg_cost: '', available_cash: '', total_assets: '' } }
const emptyTrade = (): TradeCandidate => ({ side: '', quantity: '', price: '', fees: '', time: '', reason: '' })
export function candidateFieldState(value: string): '待人工核对' | '缺失' { return value.trim() ? '待人工核对' : '缺失' }

const numeric = String.raw`([0-9][0-9,]*(?:\.[0-9]+)?)`
function cleanNumber(value: string | undefined): string { return (value || '').replace(/,/g, '') }
function labelled(text: string, labels: string[]) {
  for (const label of labels) {
    const match = text.match(new RegExp(`${label}\\s*[:：]?\\s*(?:人民币|RMB|CNY|￥|¥)?\\s*${numeric}`, 'i'))
    if (match) return { value: cleanNumber(match[1]), label, excerpt: match[0].slice(0, 80) }
  }
  return null
}
function pairedColumn(text: string, headerLeft: string, headerRight: string) {
  // Galaxy Securities displays paired columns such as ``持仓/可用`` and
  // ``成本/现价``.  Accept only an explicit paired header and always take
  // the first value.  The second value is deliberately discarded.
  const separator = String.raw`\s*(?:\/|／|\||｜)\s*`
  const match = text.match(new RegExp(`${headerLeft}${separator}${headerRight}\\s*[:：]?\\s*${numeric}${separator}${numeric}`, 'i'))
  return match ? { value: cleanNumber(match[1]), label: `${headerLeft}/${headerRight}（第一列）`, excerpt: match[0].slice(0, 100) } : null
}
function fieldEvidence(found: { label: string; excerpt: string } | null, confidence: number): FieldEvidence {
  return found ? { confidence: Math.round(Math.max(0, Math.min(99, confidence))), label: found.label, excerpt: found.excerpt } : { confidence: null, label: '未找到明确标签', excerpt: '' }
}

/** Only explicit broker labels are accepted. Positional guesses are deliberately rejected. */
export function parseBrokerScreenshot(text: string, confidence = 0): ParsedScreenshot {
  const normalized = text.replace(/，/g, ',').replace(/：/g, ':').replace(/[ \t]+/g, ' ')
  const total = labelled(normalized, ['总资产', '资产总值'])
  const cash = labelled(normalized, ['可用现金', '可用资金', '资金可用'])
  const shares = labelled(normalized, ['持仓股数', '持仓数量', '股票余额', '股份余额']) || pairedColumn(normalized, '持仓', '可用')
  const cost = labelled(normalized, ['持仓成本', '成本价', '成本']) || pairedColumn(normalized, '成本', '现价')
  const tradePrice = labelled(normalized, ['实际成交价', '成交价格', '成交均价', '成交价'])
  const tradeQty = labelled(normalized, ['成交数量', '成交股数'])
  const fee = labelled(normalized, ['手续费', '佣金'])
  const sideMatch = normalized.match(/(?:成交方向|买卖方向)\s*:?\s*(买入|卖出)/)
  const timeMatch = normalized.match(/(?:成交时间|成交日期)\s*:?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)/)
  const sideFound = sideMatch ? { label: '成交方向', excerpt: sideMatch[0] } : null
  const timeFound = timeMatch ? { label: '成交时间', excerpt: timeMatch[0] } : null
  const warnings: string[] = []
  if (/现价|最新价|市价/.test(normalized) && !tradePrice) warnings.push('截图含现价/最新价，但没有明确“成交价”标签，成交价保持空白。')
  if (confidence < 70) warnings.push('OCR整体置信度偏低，请逐字段对照原图。')
  return {
    account: { shares: shares?.value || '', avg_cost: cost?.value || '', available_cash: cash?.value || '', total_assets: total?.value || '' },
    trade: { side: sideMatch?.[1] === '买入' ? 'BUY' : sideMatch?.[1] === '卖出' ? 'SELL' : '', quantity: tradeQty?.value || '', price: tradePrice?.value || '', fees: fee?.value || '', time: timeMatch?.[1]?.replace(/[/.]/g, '-').replace(' ', 'T') || '', reason: '' },
    evidence: { shares: fieldEvidence(shares, confidence), avg_cost: fieldEvidence(cost, confidence), available_cash: fieldEvidence(cash, confidence), total_assets: fieldEvidence(total, confidence), side: fieldEvidence(sideFound, confidence), quantity: fieldEvidence(tradeQty, confidence), price: fieldEvidence(tradePrice, confidence), fees: fieldEvidence(fee, confidence), time: fieldEvidence(timeFound, confidence) }, warnings,
  }
}

function CandidateField({ label, value, onChange, hint, evidence }: { label: string; value: string; onChange: (value: string) => void; hint?: string; evidence?: FieldEvidence }) {
  const state = candidateFieldState(value)
  const detail = evidence?.confidence == null ? 'OCR未识别' : `OCR ${evidence.confidence}% · ${evidence.label}`
  return <label className="candidate-field"><span>{label}<small title={evidence?.excerpt || ''} className={`candidate-confidence candidate-confidence--${state === '缺失' ? 'missing' : 'manual'}`}>{state} · {detail}</small></span><input type="number" min="0" step="any" value={value} onChange={(event) => onChange(event.target.value)} placeholder={hint || '识别不到请留空'} /></label>
}

export function ScreenshotCandidate({ onApplyAccount, onUpdated }: { onApplyAccount: (candidate: AccountCandidate) => void; onUpdated: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [kind, setKind] = useState<ScreenshotKind>('holding')
  const [account, setAccount] = useState<AccountCandidate>(() => emptyAccountCandidate())
  const [trade, setTrade] = useState<TradeCandidate>(() => emptyTrade())
  const [evidence, setEvidence] = useState<Record<string, FieldEvidence>>({})
  const [ocrText, setOcrText] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const [progress, setProgress] = useState<number | null>(null)
  const [checked, setChecked] = useState(false)
  const [message, setMessage] = useState('')
  const preview = useMemo(() => file ? URL.createObjectURL(file) : '', [file])
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview) }, [preview])

  const runOcr = async (selected: File) => {
    setProgress(0); setMessage('正在浏览器本地识别；首次会下载简体中文模型，图片不会上传。')
    try {
      const { recognize } = await import('tesseract.js')
      const result = await recognize(selected, 'chi_sim+eng', { logger: (event) => { if (event.status === 'recognizing text' && typeof event.progress === 'number') setProgress(Math.round(event.progress * 100)) } })
      const parsed = parseBrokerScreenshot(result.data.text, result.data.confidence)
      setOcrText(result.data.text); setAccount(parsed.account); setTrade(parsed.trade); setEvidence(parsed.evidence); setWarnings(parsed.warnings)
      setMessage('本地OCR完成。自动填写值仍是候选，请逐项核对原图。')
    } catch (error) {
      setWarnings(['OCR模型下载或本地识别失败，已降级为手工录入；图片仍未上传。'])
      setMessage(error instanceof Error ? `OCR失败：${error.message}` : 'OCR失败，已降级为手工录入。')
    } finally { setProgress(null) }
  }
  const selectFile = (selected: File | null) => {
    setFile(selected); setAccount(emptyAccountCandidate()); setTrade(emptyTrade()); setEvidence({}); setOcrText(''); setWarnings([]); setChecked(false); setMessage('')
    if (selected) void runOcr(selected)
  }
  const recordTrade = async () => {
    if (!checked) { setMessage('请先逐项核对，并勾选“我确认这是实际成交”。'); return }
    if (!['BUY', 'SELL'].includes(trade.side) || !trade.quantity || !trade.price) { setMessage('成交方向、数量和成交价缺失，未记录。'); return }
    try {
      await postCoach('trade', { side: trade.side, quantity: Number(trade.quantity), price: Number(trade.price), fees: trade.fees ? Number(trade.fees) : null, execution_status: 'EXECUTED_MANUALLY', reason: `${trade.reason || '用户核对券商成交截图'}${trade.time ? `；截图成交时间：${trade.time}` : ''}` })
      setMessage('已追加为人工确认成交记录；没有发送订单。'); onUpdated()
    } catch (error) { setMessage(error instanceof Error ? error.message : '记录失败') }
  }

  const statusText = (key: string, value: string) => `${candidateFieldState(value)} · ${evidence[key]?.confidence == null ? 'OCR未识别' : `OCR ${evidence[key].confidence}% · ${evidence[key].label}`}`
  return <section className="screenshot-candidate">
    <div className="screenshot-candidate__intro"><div><b>从券商截图自动建立候选</b><p>图片只在浏览器本地处理，不上传、不保存；OCR文本仅驻留当前页面内存。首次识别可能下载简体中文模型。</p></div><label className="file-button">选择截图<input type="file" accept="image/*" capture="environment" onChange={(event) => selectFile(event.target.files?.[0] || null)} /></label></div>
    {file && <div className="screenshot-candidate__workspace"><div className="screenshot-preview"><img src={preview} alt="仅本地显示的券商截图预览" /><small>{file.name} · 原图未上传</small>{progress !== null && <progress max="100" value={progress}>{progress}%</progress>}</div><div className="candidate-editor"><div className="candidate-kind"><button type="button" className={kind === 'holding' ? 'is-active' : ''} onClick={() => setKind('holding')}>持仓/资产截图</button><button type="button" className={kind === 'trade' ? 'is-active' : ''} onClick={() => setKind('trade')}>成交截图</button></div>
      {warnings.map((warning) => <div className="candidate-warning" key={warning}>{warning}</div>)}
      {ocrText && <details className="ocr-summary"><summary>查看本地识别文本摘要（仅内存）</summary><pre>{ocrText.slice(0, 1200)}</pre></details>}
      {kind === 'holding' ? <><div className="candidate-grid"><CandidateField label="持仓股数" value={account.shares} evidence={evidence.shares} onChange={(value) => setAccount({ ...account, shares: value })} /><CandidateField label="持仓成本（元）" value={account.avg_cost} evidence={evidence.avg_cost} onChange={(value) => setAccount({ ...account, avg_cost: value })} /><CandidateField label="可用现金（元）" value={account.available_cash} evidence={evidence.available_cash} onChange={(value) => setAccount({ ...account, available_cash: value })} /><CandidateField label="总资产（元）" value={account.total_assets} evidence={evidence.total_assets} onChange={(value) => setAccount({ ...account, total_assets: value })} /></div><button type="button" className="primary-button" onClick={() => { onApplyAccount(account); setMessage('候选值已带入下方账户表单；仍需逐项核对并点击确认。') }}>带入账户确认表单</button></> : <><div className="candidate-grid"><label className="candidate-field"><span>成交方向<small title={evidence.side?.excerpt}>{statusText('side', trade.side)}</small></span><select value={trade.side} onChange={(event) => setTrade({ ...trade, side: event.target.value })}><option value="">请选择</option><option value="BUY">买入</option><option value="SELL">卖出</option></select></label><CandidateField label="成交数量（股）" value={trade.quantity} evidence={evidence.quantity} onChange={(value) => setTrade({ ...trade, quantity: value })} /><CandidateField label="实际成交价（元）" value={trade.price} evidence={evidence.price} onChange={(value) => setTrade({ ...trade, price: value })} hint="必须来自明确成交价标签，不是现价" /><CandidateField label="手续费（元，可空）" value={trade.fees} evidence={evidence.fees} onChange={(value) => setTrade({ ...trade, fees: value })} /><label className="candidate-field"><span>成交时间<small title={evidence.time?.excerpt}>{statusText('time', trade.time)}</small></span><input type="datetime-local" value={trade.time} onChange={(event) => setTrade({ ...trade, time: event.target.value })} /></label><label className="candidate-field"><span>核对说明</span><input value={trade.reason} onChange={(event) => setTrade({ ...trade, reason: event.target.value })} placeholder="可选" /></label></div><div className="candidate-warning">成交价只接受明确“成交价/成交均价/成交价格”标签；现价、最新价、市价和委托价绝不会自动填入。</div><label className="candidate-check"><input type="checkbox" checked={checked} onChange={(event) => setChecked(event.target.checked)} />我已逐项核对，并确认这是已经发生的实际成交；系统不会下单。</label><button type="button" className="primary-button" onClick={() => void recordTrade()}>确认并追加成交记录</button></>}
    </div></div>}
    {message && <div className="form-message" role="status">{message}</div>}
  </section>
}
