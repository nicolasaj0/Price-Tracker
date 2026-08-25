"""Módulo de Renderização de Gráficos de Histórico de Preços para Discord.
Suporte Multi-Região / Multi-Moeda (BRL, USD, EUR, GBP, JPY, CAD, ARS) e baixo consumo de RAM.
"""

from datetime import datetime
import gc
import io
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger("PriceTracker.Chart")


def _format_axis_price(val: float, currency: str, country_code: str) -> str:
    """Formata valor numérico para exibição nos eixos e anotações do gráfico."""
    curr = currency.upper().strip()
    cc = country_code.upper().strip()

    if curr == "BRL" or cc == "BR":
        return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    elif curr == "USD":
        if cc == "AR":
            return f"${val:,.2f} USD"
        return f"${val:,.2f}"
    elif curr == "EUR":
        return f"{val:,.2f} €".replace(".", ",")
    elif curr == "GBP":
        return f"£{val:,.2f}"
    elif curr == "JPY":
        return f"¥{val:,.0f}"
    elif curr == "CAD":
        return f"CA${val:,.2f}"
    elif curr == "AUD":
        return f"AU${val:,.2f}"
    return f"{curr} {val:,.2f}"


def gerar_grafico_historico(
    titulo: str,
    historico: List[Tuple[str, float]],
    currency: str = "BRL",
    country_code: str = "BR",
) -> Optional[io.BytesIO]:
    """Gera um gráfico PNG em memória com o histórico de preços do jogo na moeda da região.

    Args:
        titulo: Nome do jogo para exibição no topo do gráfico.
        historico: Lista cronológica de tuplas (data_iso, preco_float).
        currency: Código ISO da moeda (BRL, USD, EUR, etc.).
        country_code: Código ISO-2 do país (BR, US, PT, etc.).

    Returns:
        BytesIO contendo os bytes do PNG ou None se houver dados insuficientes.
    """
    if not historico or len(historico) < 2:
        return None

    # Lazy import do matplotlib para poupar memória RAM no startup
    import matplotlib
    matplotlib.use("Agg")  # Backend headless sem interface de janelas
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    fig = None
    ax = None
    buffer = io.BytesIO()

    try:
        dates = []
        prices = []
        for dt_str, p in historico:
            try:
                clean_dt = dt_str.replace("Z", "+00:00")
                if "T" in clean_dt:
                    dt = datetime.fromisoformat(clean_dt)
                else:
                    dt = datetime.strptime(clean_dt.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                dt = datetime.utcnow()
            dates.append(dt)
            prices.append(float(p))

        # Adiciona espaçamento temporal se todas as datas forem no mesmo segundo
        if len(set(dates)) == 1:
            from datetime import timedelta
            dates = [dates[0] + timedelta(minutes=i * 10) for i in range(len(dates))]

        # Configurações visuais ultra-otimizadas para baixo consumo de RAM (< 2 MB)
        fig, ax = plt.subplots(figsize=(5.6, 2.6), dpi=80)
        fig.patch.set_facecolor("#2B2D31")  # Fundo externo Discord
        ax.set_facecolor("#1E1F22")        # Fundo do gráfico

        # Plotagem da linha principal de preço
        line_color = "#5865F2"  # Discord Blurple
        ax.plot(
            dates,
            prices,
            color=line_color,
            linewidth=2.0,
            marker="o",
            markersize=4.5,
            markerfacecolor="#FFFFFF",
            markeredgecolor=line_color,
            markeredgewidth=1.2,
            label="Preço Registrado",
            zorder=3,
        )

        # Preenchimento translúcido sob a curva
        ax.fill_between(dates, prices, color=line_color, alpha=0.18, zorder=2)

        # Destaque do Menor Preço Histórico
        min_price = min(prices)
        min_idx = prices.index(min_price)
        min_date = dates[min_idx]

        ax.plot(
            min_date,
            min_price,
            marker="o",
            markersize=7,
            markerfacecolor="#57F287",  # Discord Green
            markeredgecolor="#1E1F22",
            markeredgewidth=1.5,
            zorder=4,
        )

        min_price_label = _format_axis_price(min_price, currency, country_code)
        ax.annotate(
            f"Menor: {min_price_label}",
            xy=(min_date, min_price),
            xytext=(0, 12),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8.5,
            fontweight="bold",
            color="#57F287",
            bbox=dict(
                boxstyle="round,pad=0.3",
                fc="#2B2D31",
                ec="#57F287",
                lw=1.0,
                alpha=0.95,
            ),
            zorder=5,
        )

        # Título com bandeira/país
        title_display = titulo if len(titulo) <= 32 else titulo[:29] + "..."
        ax.set_title(
            f"Histórico ({country_code.upper()}): {title_display}",
            color="#F2F3F5",
            fontsize=10.0,
            fontweight="bold",
            pad=10,
        )

        # Formatação do Eixo X (Datas no padrão DD/MM)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        fig.autofmt_xdate(rotation=0, ha="center")

        # Formatação do Eixo Y Dinâmica por Moeda
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda x, pos: _format_axis_price(x, currency, country_code))
        )

        # Limites e Margens do Eixo Y
        y_min = max(0, min_price * 0.85)
        y_max = max(prices) * 1.15 if max(prices) > min_price else min_price * 1.25 + 5.0
        ax.set_ylim(y_min, y_max)

        for spine in ax.spines.values():
            spine.set_color("#3F4147")
            spine.set_linewidth(0.8)

        ax.tick_params(colors="#949BA4", labelsize=8.0)
        ax.grid(True, linestyle="--", alpha=0.3, color="#4E5058", zorder=1)

        plt.tight_layout()

        fig.savefig(
            buffer,
            format="png",
            facecolor=fig.get_facecolor(),
            edgecolor="none",
            dpi=80,
        )
        buffer.seek(0)
        return buffer

    except Exception as exc:
        logger.error("Erro ao gerar gráfico multi-moeda: %s", exc, exc_info=True)
        return None

    finally:
        if fig is not None:
            plt.close(fig)
        plt.close("all")
        del fig, ax
        gc.collect()
        gc.collect()
