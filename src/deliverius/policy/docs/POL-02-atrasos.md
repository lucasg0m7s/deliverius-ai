# POL-02 · Atrasos

## Quando se aplica

Quando o pedido chega, ou ainda não chegou, depois da previsão de entrega mostrada no app.

## O que o Deliverius faz

O atraso é contado a partir da previsão de entrega.

- **Atraso de {{minor_delay}} a {{major_delay}}:** você recebe um cupom de {{minor_delay_coupon_percent}} do
  valor do pedido, de no máximo {{minor_delay_coupon_cap}}.
- **Atraso de mais de {{major_delay}}:** você recebe um cupom de {{major_delay_coupon}}. Se o pedido ainda não saiu
  para entrega, você pode escolher cancelar e receber de volta o valor total.
- **Atraso menor que {{minor_delay}}:** está dentro da margem normal da entrega e não gera compensação.

Cada pedido recebe uma única compensação por atraso.

## Prazo

Você pode reclamar do atraso enquanto o pedido está em andamento ou até {{late_claim_window}} depois da entrega.

## Quando uma pessoa analisa

Um cupom acima de {{coupon_auto_approval_limit}} ou um reembolso acima de {{auto_approval_limit}} é analisado por
uma pessoa da nossa equipe.
