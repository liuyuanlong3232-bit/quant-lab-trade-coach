import { describe, expect, it } from 'vitest'
import { candidateFieldState, emptyAccountCandidate, parseBrokerScreenshot } from './ScreenshotCandidate'

describe('截图候选录入', () => {
  it('未识别字段必须保持空值，不能伪造零值或默认成交价', () => {
    expect(emptyAccountCandidate()).toEqual({ shares: '', avg_cost: '', available_cash: '', total_assets: '' })
  })

  it('只把用户填写的值标记为待核对，不宣称 OCR 已确认', () => {
    expect(candidateFieldState('')).toBe('缺失')
    expect(candidateFieldState('  ')).toBe('缺失')
    expect(candidateFieldState('600')).toBe('待人工核对')
  })

  it('从银河证券持仓标签提取候选且保留证据', () => {
    const parsed = parseBrokerScreenshot('银河证券 总资产 34,668.25 可用资金 9,720.25 持仓数量 600 成本价 34.751 现价 41.58', 88)
    expect(parsed.account).toEqual({ shares: '600', avg_cost: '34.751', available_cash: '9720.25', total_assets: '34668.25' })
    expect(parsed.evidence.avg_cost.label).toBe('成本价')
    expect(parsed.evidence.avg_cost.confidence).toBe(88)
  })

  it('绝不把现价、最新价或委托价当作成交价', () => {
    const parsed = parseBrokerScreenshot('成交方向 买入 成交数量 100 委托价 35.20 现价 41.58 最新价 41.60', 92)
    expect(parsed.trade.quantity).toBe('100')
    expect(parsed.trade.price).toBe('')
    expect(parsed.warnings.join(' ')).toContain('成交价保持空白')
  })

  it('仅在明确成交标签存在时填写成交候选', () => {
    const parsed = parseBrokerScreenshot('成交方向:卖出 成交数量:200 成交均价:40.690 手续费:5.20 成交时间:2026-08-27 14:59:45', 83)
    expect(parsed.trade).toMatchObject({ side: 'SELL', quantity: '200', price: '40.690', fees: '5.20', time: '2026-08-27T14:59:45' })
  })

  it('解析真实银河证券持仓页OCR的分字和双行表格', () => {
    const text = `
中 国 银河 证 券
总 资产 浮动 盈亏 当日 参考 盈亏
32,885.52 +2,314.59 -1,770.00 -5.11%
总 市 信 可 用 逆 回 购 可 取 转账
7,726.00 25,158.52 --
市值 盈亏 持仓 /可 用 世 本 /现价
兴业 银 锡 2,314.59 200 27.057
7,726.00 42.772% 200 38.630
`
    const parsed = parseBrokerScreenshot(text, 72)
    expect(parsed.account).toEqual({ shares: '200', avg_cost: '27.057', available_cash: '25158.52', total_assets: '32885.52' })
    expect(parsed.trade.price).toBe('')
    expect(parsed.evidence.avg_cost.label).toContain('成本/现价表')
  })

  it('保守解析银河证券双列表头，只取持仓和成本第一列', () => {
    const text = `银河证券\n总资产 34,668.25\n可用资金 9,720.25\n兴业银锡 000426\n持仓/可用 600/600\n成本/现价 34.751/38.630`
    const parsed = parseBrokerScreenshot(text, 86)
    expect(parsed.account).toEqual({ shares: '600', avg_cost: '34.751', available_cash: '9720.25', total_assets: '34668.25' })
    expect(parsed.evidence.shares.label).toBe('持仓/可用（第一列）')
    expect(parsed.evidence.avg_cost.label).toBe('成本/现价（第一列）')
    expect(parsed.trade.price).toBe('')
  })

  it('没有明确组合表头时不按位置猜测双列数值', () => {
    const parsed = parseBrokerScreenshot('兴业银锡 000426 600 600 34.751 38.630 现价 38.630', 90)
    expect(parsed.account.shares).toBe('')
    expect(parsed.account.avg_cost).toBe('')
    expect(parsed.trade.price).toBe('')
  })
})
