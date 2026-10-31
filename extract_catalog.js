() => {
  const clean = e => (e?.textContent || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.cart-products-box[pid]')).map(e => ({
    pid: e.getAttribute('pid'),
    title: clean(e.querySelector('.cart-products-title')),
    out_of_stock: e.classList.contains('none-stock'),
    stock_text: clean(e.querySelector('.cart-products-qty')),
    amount: clean(e.querySelector('.cart-products-price .price-num')),
    currency: clean(e.querySelector('.cart-products-price .price-suffix')),
    cycle: clean(e.querySelector('.cart-products-price .billing-cycle-text')),
    setup: clean(e.querySelector('.cart-products-setup-fee'))
  }));
}
