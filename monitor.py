"""DMIT public-cart monitor. No DMIT login, checkout, or purchase is performed."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parent
CART = 'https://www.dmit.io/cart.php'
STATE = Path(os.getenv('STATE_FILE', 'data/state.json'))
MONTH_LIMIT = Decimal('20')
YEAR_LIMIT = Decimal('100')
WEST_COAST = {'LAX', 'SJC', 'SEA', 'SFO'}


class CheckError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_state(path=STATE):
    if not path.exists():
        return {'products': {}, 'failures': 0, 'failure_notified': False}
    state = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state.get('products'), dict):
        raise CheckError('状态文件无效；保留原文件，未重置提醒记录。')
    return state


def in_scope(title):
    tokens = title.upper().split('.')
    return tokens[0] in WEST_COAST and any(t in {'PRO', 'PREMIUM'} for t in tokens[1:])


def parse_amount(text):
    # Require USD explicitly: a currency symbol alone could mean another dollar.
    match = re.search(r'\$?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s+USD\b', text, re.I)
    if not match:
        raise CheckError('计费选项缺少明确的美元金额。')
    try:
        value = Decimal(match.group(1).replace(',', ''))
    except InvalidOperation as error:
        raise CheckError('无法识别计费金额。') from error
    if value <= 0:
        raise CheckError('计费金额为零或无效，需人工核实。')
    return str(value)


def parse_prices(options):
    prices = {}
    for option in options:
        if option['value'] in {'monthly', 'annually'} and not option.get('disabled'):
            prices[option['value']] = parse_amount(option['text'])
    if not prices:
        raise CheckError('未找到可核实的月付或年付选项。')
    return prices


def qualifying_prices(prices):
    return {cycle: amount for cycle, amount in prices.items()
            if cycle in {'monthly', 'annually'} and Decimal(amount) > 0
            and Decimal(amount) < (MONTH_LIMIT if cycle == 'monthly' else YEAR_LIMIT)}


def push_payload(title, body, url=CART, critical=True):
    payload = {'device_key': os.getenv('BARK_KEY', ''), 'title': title,
               'body': body, 'url': url, 'group': 'DMIT库存', 'isArchive': '1',
               'level': 'critical' if critical else 'active'}
    if critical:
        payload.update({'sound': 'alarm', 'volume': '10', 'call': '1'})
    return payload


def validate_push_config():
    key = os.getenv('BARK_KEY', '').strip()
    # Accept the copied default Bark URL so phone users need not extract the key.
    if key.startswith('https://'):
        address = urlsplit(key)
        if address.hostname != 'api.day.app' or address.username or address.port not in {None, 443}:
            raise CheckError('完整推送链接必须来自 https://api.day.app；自建服务请单独配置 BARK_SERVER。')
        key = address.path.strip('/').split('/')[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', key):
        raise CheckError('请在 GitHub Actions Secret BARK_KEY 中填写设备密钥或 Bark 首页复制的完整推送链接。')
    server = os.getenv('BARK_SERVER', 'https://api.day.app').rstrip('/')
    parsed = urlsplit(server)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise CheckError('BARK_SERVER 必须是无用户名、查询参数的 HTTPS 服务地址。')
    return key, server


def send_push(title, body, url=CART, critical=True):
    key, server = validate_push_config()
    payload = push_payload(title, body, url, critical)
    payload['device_key'] = key
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(server + '/push', data=data,
                                     headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
        if result.get('code') != 200:
            raise CheckError('Bark 服务未接受推送；下次检查会重试。')
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        # Never print the key or the request body.
        raise CheckError('Bark 推送失败；请核实密钥与网络，下次检查会重试。') from error


def describe(record):
    prices = record.get('prices', {})
    parts = []
    for cycle, label in [('monthly', '月付'), ('annually', '年付')]:
        if cycle in prices:
            parts.append(f'{label} ${prices[cycle]} USD')
    return f"{record['title']}\nPremium · 美国西部\n" + '；'.join(parts)


def apply_records(state, records, notify=send_push):
    """Unknown/missing products retain prior state. Failed pushes remain retryable."""
    sent = 0
    for record in records:
        pid = record['pid']
        if record.get('error'):
            continue
        matches = qualifying_prices(record.get('prices', {})) if record['in_stock'] else {}
        fingerprint = json.dumps(matches, sort_keys=True) if matches else ''
        previous = state['products'].get(pid, {})
        if fingerprint and previous.get('notified') != fingerprint:
            notify('DMIT服务器已经开放', describe(record) + '\n库存与价格以最终结账页面为准。',
                   CART + '?' + urlencode({'a': 'add', 'pid': pid}))
            sent += 1
        state['products'][pid] = {**record, 'notified': fingerprint, 'checked_at': utcnow()}
    return sent


def collect():
    # Lazy import keeps logic tests and --test-push independent of a browser.
    from playwright.sync_api import sync_playwright

    results = []
    errors = []
    with sync_playwright() as p:
        profile = os.getenv('BROWSER_PROFILE')
        launch = dict(headless=os.getenv('HEADLESS', '0') == '1',
                      channel=os.getenv('BROWSER_CHANNEL', 'chromium'))
        if os.getenv('BROWSER_MINIMIZE') == '1':
            launch['args'] = ['--start-minimized']
        if profile:
            context = p.chromium.launch_persistent_context(profile, locale='en-US', **launch)
            close_browser = context.close
        else:
            browser = p.chromium.launch(**launch)
            context = browser.new_context(locale='en-US')
            close_browser = browser.close
        try:
            page = context.new_page()
            page.set_default_timeout(45000)
            response = page.goto(CART, wait_until='domcontentloaded', timeout=60000)
            # Let an ordinary browser finish normal automatic JS loading before
            # deciding that an initial HTTP error makes the site unreadable.
            try:
                page.locator('.cart-products-box[pid] .cart-products-title').first.wait_for(state='attached')
            except Exception as error:
                status = response.status if response else 'unknown'
                raise CheckError(f'购物车无法加载（HTTP {status}）；页面标题：{page.title()[:100]}。不能判断库存。') from error
            raw = page.evaluate((ROOT / 'extract_catalog.js').read_text(encoding='utf-8'))
            candidates = [r for r in raw if in_scope(r['title'])]
            if not candidates:
                raise CheckError('未读到美国西部 Premium 套餐；可能是验证码或页面结构变化。')
            if len({r['pid'] for r in candidates}) != len(candidates):
                raise CheckError('商品编号重复；停止本次库存判断。')
            detail = context.new_page()
            detail.set_default_timeout(30000)
            for row in candidates:
                if not row['pid'].isdigit() or not row['title']:
                    raise CheckError('商品编号或标题缺失。')
                record = {**row, 'in_stock': not row['out_of_stock'], 'prices': {}}
                if row['out_of_stock']:
                    results.append(record)
                    continue
                try:
                    if re.search(r'out of stock|sold out|unavailable|缺货|售罄', row['stock_text'], re.I):
                        raise CheckError('库存文字与商品状态不一致。')
                    # This opens an anonymous configuration page, never the checkout.
                    target = CART + '?' + urlencode({'a': 'add', 'pid': row['pid']})
                    reply = detail.goto(target, wait_until='domcontentloaded', timeout=45000)
                    if reply and reply.status >= 400:
                        raise CheckError(f'配置页面返回 HTTP {reply.status}。')
                    detail.locator('#inputBillingcycle').wait_for(state='attached')
                    names = detail.locator('.cart-products-title').all_text_contents()
                    if row['title'] not in [n.strip() for n in names]:
                        raise CheckError('配置页商品名称不匹配，不能确认可购买。')
                    options = detail.locator('#inputBillingcycle option').evaluate_all(
                        '(es) => es.map(e => ({value:e.value,text:e.textContent.trim(),disabled:e.disabled}))')
                    record['prices'] = parse_prices(options)
                except Exception as error:
                    # Browser exceptions can contain arbitrary page text; log only the type.
                    record['error'] = str(error) if isinstance(error, CheckError) else type(error).__name__
                    errors.append(f"{row['title']}: {record['error']}")
                results.append(record)
        finally:
            close_browser()
    return results, errors


def run(check_only=False, collector=collect, notifier=send_push, state_path=STATE):
    state = load_state(state_path)
    state['last_attempt'] = utcnow()
    try:
        records, errors = collector()
        if not records:
            raise CheckError('没有读到商品，不能将空结果当成缺货。')
        save(state_path.parent / 'latest.json', {'checked_at': utcnow(), 'products': records, 'errors': errors})
        if not check_only:
            # Persist each successful push immediately, even if a later product fails.
            for record in records:
                apply_records(state, [record], notifier)
                save(state_path, state)
        matches = [r for r in records if not r.get('error') and r['in_stock']
                   and qualifying_prices(r.get('prices', {}))]
        print(f'{utcnow()} Premium 商品 {len(records)}；符合预算且已核实可选购 {len(matches)}；检查错误 {len(errors)}')
        for record in records:
            amount = f"${record['amount']} {record['currency']} {record['cycle']}"
            print(record['title'], '检查失败' if record.get('error') else ('可选购' if record['in_stock'] else '缺货'), amount,
                  json.dumps(record.get('prices', {}), ensure_ascii=False))
        if errors:
            raise CheckError('；'.join(errors))
        was_notified = state.get('failure_notified', False)
        state.update({'failures': 0, 'failure_notified': False, 'last_success': utcnow(), 'last_error': None})
        if was_notified and not check_only:
            try:
                notifier('DMIT监控已恢复', '已重新成功读取美国西部 Premium 库存。', critical=False)
            except CheckError:
                pass
        save(state_path, state)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, CheckError) else type(error).__name__
        state['failures'] = state.get('failures', 0) + 1
        state['last_error'] = message
        if state['failures'] >= 3 and not state.get('failure_notified') and not check_only:
            try:
                notifier('DMIT监控需要检查', '连续三次未能完整核实库存。请查看 GitHub Actions 日志。\n' + message[:500], critical=False)
                state['failure_notified'] = True
            except CheckError:
                pass
        save(state_path, state)
        print('检查失败：' + message, file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['scan', 'check', 'test'], default='scan')
    args = parser.parse_args()
    if args.mode == 'test':
        send_push('DMIT提醒测试（并非补货）', '这是测试铃声。请确认锁屏、静音和专注模式下仍能听见；当前未宣称有货。')
        print('Bark 服务已接受测试推送；是否实际响铃需在 iPhone 上确认。')
        return 0
    if args.mode != 'check' and not os.getenv('BARK_KEY', '').strip():
        print('尚未设置 BARK_KEY，监控未启用。请先配置 GitHub Actions Secret。', file=sys.stderr)
        return 2
    if args.mode != 'check':
        try:
            validate_push_config()
        except CheckError as error:
            print(str(error), file=sys.stderr)
            return 2
    return run(check_only=args.mode == 'check')


if __name__ == '__main__':
    sys.exit(main())
