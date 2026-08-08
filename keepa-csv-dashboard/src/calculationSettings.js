const SHIPPING_TOTAL_KEY = 'keepaShippingTotal';
const PURCHASE_QUANTITY_KEY = 'keepaPurchaseQuantity';

export const DEFAULT_SHIPPING_TOTAL = 1500;
export const DEFAULT_PURCHASE_QUANTITY = 1;

export const loadShippingSettings = () => {
    const shippingTotal = Number(window.localStorage.getItem(SHIPPING_TOTAL_KEY));
    const purchaseQuantity = Number(window.localStorage.getItem(PURCHASE_QUANTITY_KEY));

    return {
        shippingTotal: Number.isFinite(shippingTotal) && shippingTotal >= 0 ? shippingTotal : DEFAULT_SHIPPING_TOTAL,
        purchaseQuantity: Number.isFinite(purchaseQuantity) && purchaseQuantity > 0
            ? purchaseQuantity
            : DEFAULT_PURCHASE_QUANTITY,
    };
};

export const saveShippingSettings = (shippingTotal, purchaseQuantity) => {
    window.localStorage.setItem(SHIPPING_TOTAL_KEY, String(shippingTotal));
    window.localStorage.setItem(PURCHASE_QUANTITY_KEY, String(purchaseQuantity));
};

export const getShippingPerItem = (shippingTotal, purchaseQuantity) => {
    const total = Number(shippingTotal);
    const quantity = Number(purchaseQuantity);
    if (!Number.isFinite(total) || total < 0 || !Number.isFinite(quantity) || quantity <= 0) return null;
    return total / quantity;
};