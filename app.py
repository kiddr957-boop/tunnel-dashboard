import datetime
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Feedstock Dashboard", layout="wide")

CATEGORIES = [
    "Paper & Cardboard", "Wood", "Plastic film", "Dense plastics - HDPE/PE",
    "Dense plastics - PET", "Dense plastics - PVC", "Mixed dense plastics",
    "Textiles", "Misc. Combustibles", "Nappies", "Misc. Non Combustibles",
    "Glass", "FE metals", "Non FE metals", "Food Waste", "Garden Waste",
    "Other putrescibles", "WEEE", "Household hazardous", "Fines (<20mm)"
]


# ── Database ──────────────────────────────────────────────────────────────────

@st.cache_resource
def get_engine():
    db = st.secrets["postgresql"]
    url = (
        f"postgresql+psycopg2://{db['user']}:{db['password']}"
        f"@{db['host']}:{db.get('port', 5432)}/{db['dbname']}"
    )
    return create_engine(url, pool_pre_ping=True)


def ensure_tables():
    with get_engine().begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS feedstock_inputs (
                sample_name TEXT NOT NULL,
                category    TEXT NOT NULL,
                wt_pct      FLOAT,
                updated_by  TEXT,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS feedstock_samples (
                sample_name   TEXT PRIMARY KEY,
                date_received DATE,
                client_ref    TEXT,
                ahk_ref       TEXT,
                updated_by    TEXT,
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """))


@st.cache_data(ttl=60)
def load_inputs():
    with get_engine().begin() as conn:
        return pd.read_sql(
            text("SELECT * FROM feedstock_inputs ORDER BY sample_name, category"), conn
        )


@st.cache_data(ttl=60)
def load_metadata():
    with get_engine().begin() as conn:
        return pd.read_sql(
            text("SELECT * FROM feedstock_samples ORDER BY sample_name"), conn
        )


def save_inputs(sample_name, user_name, rows):
    df = pd.DataFrame(rows)
    df["sample_name"] = sample_name
    df["updated_by"] = user_name or None
    df["wt_pct"] = df["wt_pct"].astype(float)
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM feedstock_inputs WHERE sample_name = :s"), {"s": sample_name}
        )
        conn.execute(
            text("""INSERT INTO feedstock_inputs (sample_name, category, wt_pct, updated_by)
                    VALUES (:sample_name, :category, :wt_pct, :updated_by)"""),
            df.to_dict("records"),
        )
    load_inputs.clear()


def save_metadata(sample_name, date_received, client_ref, ahk_ref, user_name):
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO feedstock_samples (sample_name, date_received, client_ref, ahk_ref, updated_by)
            VALUES (:sn, :dr, :cr, :ar, :ub)
            ON CONFLICT (sample_name) DO UPDATE SET
                date_received = EXCLUDED.date_received,
                client_ref    = EXCLUDED.client_ref,
                ahk_ref       = EXCLUDED.ahk_ref,
                updated_by    = EXCLUDED.updated_by,
                updated_at    = NOW()
        """), {"sn": sample_name, "dr": date_received,
               "cr": client_ref or None, "ar": ahk_ref or None, "ub": user_name or None})
    load_metadata.clear()


# ── Excel parser ──────────────────────────────────────────────────────────────

def parse_excel(file):
    """
    Reads the standard lab report layout:
      Date Received  | date  | date  | …
      Client Ref.:   | name  | name  | …
      AHK Ref.:      | ref   | ref   | …
      Material category | Wt % | …
      <category>     | val   | val   | …
      Total          | 100   | 100   | …
    Returns (parsed_dict, meta_dict, error_str).
    """
    try:
        raw = pd.read_excel(file, header=None, dtype=str)
    except Exception as e:
        return None, None, str(e)

    scraped = {}
    data_start = None
    for i, row in raw.iterrows():
        cell = " ".join(str(row.iloc[0]).strip().replace("\xa0", " ").split())
        vals = [
            str(v).strip() for v in row.iloc[1:]
            if pd.notna(v) and str(v).strip() not in ("", "nan")
        ]
        if "Date" in cell and "Received" in cell:
            scraped["dates"] = vals
        elif "Client" in cell and "Ref" in cell:
            scraped["client_refs"] = vals
        elif "AHK" in cell and "Ref" in cell:
            scraped["ahk_refs"] = vals
        elif cell == "Material category":
            data_start = i + 1
            break

    if data_start is None:
        return None, None, "Could not find a 'Material category' row."

    n = max((len(v) for v in scraped.values()), default=1)
    refs = scraped.get("client_refs") or [f"Col {j + 1}" for j in range(n)]

    meta_out = {
        ref: {
            "date_received": (scraped.get("dates") or [None])[j]
                             if j < len(scraped.get("dates") or []) else None,
            "ahk_ref": (scraped.get("ahk_refs") or [None])[j]
                       if j < len(scraped.get("ahk_refs") or []) else None,
        }
        for j, ref in enumerate(refs)
    }

    parsed = {ref: {} for ref in refs}
    for i in range(data_start, len(raw)):
        cat = str(raw.iloc[i, 0]).strip()
        if not cat or cat.lower() in ("total", "nan"):
            continue
        if cat not in CATEGORIES:
            continue
        for j, ref in enumerate(refs):
            try:
                parsed[ref][cat] = float(raw.iloc[i, j + 1])
            except (ValueError, TypeError, IndexError):
                parsed[ref][cat] = 0.0

    return parsed, meta_out, None


# ── Startup ───────────────────────────────────────────────────────────────────

try:
    ensure_tables()
except Exception as e:
    st.error(f"DB init error: {e}")
    st.stop()

try:
    all_inputs = load_inputs()
    all_meta   = load_metadata()
except Exception as e:
    st.error(f"Database error: {e}")
    st.stop()

saved_samples = (
    sorted(all_inputs["sample_name"].unique().tolist()) if not all_inputs.empty else []
)

# ── Page ──────────────────────────────────────────────────────────────────────

st.title("Feedstock Composition Dashboard")

left, right = st.columns([2, 3], gap="large")

# ═══════════════════════════════════════════════════════════
# LEFT — Sample editor
# ═══════════════════════════════════════════════════════════

with left:
    st.subheader("Edit sample")

    sel = st.selectbox("Sample", saved_samples + ["＋ New sample"])
    if sel == "＋ New sample":
        sample_name = st.text_input("Sample name", placeholder="e.g. S1 or Sample 1 MSW - 01")
    else:
        sample_name = sel

    # Reset inputs when the active sample changes
    if st.session_state.get("_editing") != sample_name:
        prev = st.session_state.get("_editing")
        if prev:
            for cat in CATEGORIES:
                st.session_state.pop(f"inp_{prev}_{cat}", None)
        st.session_state["_editing"] = sample_name

    # Metadata fields — pre-filled from DB where available
    meta_row = {}
    if not all_meta.empty and sample_name in all_meta["sample_name"].values:
        meta_row = all_meta.loc[all_meta["sample_name"] == sample_name].iloc[0].to_dict()

    m1, m2 = st.columns(2)
    with m1:
        dr_default = (
            pd.to_datetime(meta_row["date_received"]).date()
            if meta_row.get("date_received") is not None
            else datetime.date.today()
        )
        date_received = st.date_input("Date received", value=dr_default, key=f"dr_{sample_name}")
        client_ref    = st.text_input("Client ref", value=meta_row.get("client_ref") or "",
                                      key=f"cr_{sample_name}")
    with m2:
        ahk_ref   = st.text_input("AHK ref",    value=meta_row.get("ahk_ref") or "",
                                   key=f"ar_{sample_name}")
        user_name = st.text_input("Your name",  placeholder="Optional", key=f"un_{sample_name}")

    st.divider()

    if not sample_name.strip():
        st.info("Enter a sample name above to start.")
    else:
        # Composition inputs — live (outside form so total updates on every keystroke)
        existing_vals = {}
        if not all_inputs.empty:
            mask = all_inputs["sample_name"] == sample_name
            existing_vals = dict(zip(
                all_inputs.loc[mask, "category"],
                all_inputs.loc[mask, "wt_pct"],
            ))

        c1, c2 = st.columns(2)
        for i, cat in enumerate(CATEGORIES):
            with (c1 if i % 2 == 0 else c2):
                st.number_input(
                    cat, min_value=0.0, max_value=100.0, step=0.1,
                    value=float(existing_vals.get(cat, 0.0)),
                    key=f"inp_{sample_name}_{cat}",
                )

        total = sum(
            st.session_state.get(f"inp_{sample_name}_{cat}", 0.0) for cat in CATEGORIES
        )
        if abs(total - 100.0) <= 0.5:
            st.success(f"Total: {total:.2f}%")
        elif abs(total - 100.0) <= 3.0:
            st.warning(f"Total: {total:.2f}% — slightly off 100%")
        else:
            st.error(f"Total: {total:.2f}% — does not sum to 100%")

        if st.button("Save", type="primary", use_container_width=True):
            rows = [
                {"category": cat,
                 "wt_pct": st.session_state.get(f"inp_{sample_name}_{cat}", 0.0)}
                for cat in CATEGORIES
            ]
            save_inputs(sample_name, user_name.strip(), rows)
            save_metadata(
                sample_name, date_received,
                client_ref.strip(), ahk_ref.strip(), user_name.strip(),
            )
            st.success(f"Saved {sample_name}.")
            st.rerun()

# ═══════════════════════════════════════════════════════════
# RIGHT — Compare / Charts / Import
# ═══════════════════════════════════════════════════════════

with right:
    tab_compare, tab_charts, tab_import = st.tabs(["Compare", "Charts", "Import Excel"])

    # ── Compare ───────────────────────────────────────────

    with tab_compare:
        if all_inputs.empty:
            st.info("No samples saved yet.")
        else:
            wide = (
                all_inputs
                .pivot_table(index="category", columns="sample_name",
                             values="wt_pct", aggfunc="last")
                .reindex(CATEGORIES)
            )
            wide.index.name = "Category"

            avg_sel = st.multiselect(
                "Include in average:", saved_samples,
                default=saved_samples, key="avg_sel",
            )
            if avg_sel:
                present = [c for c in avg_sel if c in wide.columns]
                if present:
                    wide["Average"] = wide[present].mean(axis=1)

            totals  = wide.sum().rename("TOTAL")
            display = pd.concat([wide, totals.to_frame().T])

            st.dataframe(
                display.style.format("{:.2f}", na_rep="—"),
                use_container_width=True,
                height=660,
            )

            if not all_meta.empty:
                with st.expander("Sample metadata"):
                    show_cols = [c for c in
                                 ["sample_name", "date_received", "client_ref",
                                  "ahk_ref", "updated_by", "updated_at"]
                                 if c in all_meta.columns]
                    st.dataframe(
                        all_meta[show_cols].set_index("sample_name"),
                        use_container_width=True,
                    )

            st.download_button(
                "Download CSV",
                data=all_inputs.to_csv(index=False),
                file_name="feedstock_samples.csv",
                mime="text/csv",
            )

    # ── Charts ────────────────────────────────────────────

    with tab_charts:
        if all_inputs.empty:
            st.info("No samples saved yet.")
        else:
            chart_sel  = st.multiselect(
                "Samples:", saved_samples, default=saved_samples, key="chart_sel"
            )
            chart_type = st.radio(
                "Chart type", ["Stacked bar", "Grouped bar"], horizontal=True
            )

            if chart_sel:
                chart_df = all_inputs[all_inputs["sample_name"].isin(chart_sel)].copy()
                chart_df["category"] = pd.Categorical(
                    chart_df["category"], categories=CATEGORIES, ordered=True
                )
                chart_df = chart_df.sort_values("category")

                fig = px.bar(
                    chart_df,
                    x="sample_name", y="wt_pct", color="category",
                    barmode="stack" if chart_type == "Stacked bar" else "group",
                    labels={"sample_name": "Sample", "wt_pct": "wt%", "category": "Category"},
                    color_discrete_sequence=px.colors.qualitative.Dark24,
                    height=530,
                )
                fig.update_layout(
                    legend=dict(orientation="v", x=1.01, y=1, font_size=11),
                    margin=dict(r=220),
                    plot_bgcolor="#0e1116",
                    paper_bgcolor="#0e1116",
                    font_color="#fafafa",
                )
                st.plotly_chart(fig, use_container_width=True)

    # ── Import Excel ──────────────────────────────────────

    with tab_import:
        st.caption(
            "Upload a lab report .xlsx. Reads **Date Received**, **Client Ref.:** and "
            "**AHK Ref.:** header rows, then the **Material category** data block."
        )

        uploaded = st.file_uploader("Choose .xlsx", type=["xlsx", "xls"])

        if uploaded:
            parsed, meta_out, err = parse_excel(uploaded)
            if err:
                st.error(err)
            else:
                st.success(f"Found **{len(parsed)}** sample column(s).")

                preview = pd.DataFrame(
                    [{"Category": cat,
                      **{ref: vals.get(cat, 0.0) for ref, vals in parsed.items()}}
                     for cat in CATEGORIES]
                ).set_index("Category")
                st.dataframe(preview.style.format("{:.2f}"), use_container_width=True)

                st.markdown("**Name each column (leave blank to skip):**")
                imp_cols = st.columns(min(len(parsed), 3))
                imp_map  = {}
                for i, ref in enumerate(parsed):
                    with imp_cols[i % len(imp_cols)]:
                        name = st.text_input(ref, value=ref, key=f"impname_{i}")
                        imp_map[ref] = name.strip()

                if st.button("Import", type="primary"):
                    done = []
                    for ref, target in imp_map.items():
                        if not target:
                            continue
                        rows = [
                            {"category": c, "wt_pct": parsed[ref].get(c, 0.0)}
                            for c in CATEGORIES
                        ]
                        save_inputs(target, "Excel import", rows)
                        m  = meta_out.get(ref, {})
                        try:
                            dr = pd.to_datetime(m["date_received"]).date() \
                                 if m.get("date_received") else None
                        except Exception:
                            dr = None
                        save_metadata(target, dr, ref, m.get("ahk_ref"), "Excel import")
                        for cat in CATEGORIES:
                            st.session_state.pop(f"inp_{target}_{cat}", None)
                        done.append(target)
                    if done:
                        st.success("Imported: " + ", ".join(done))
                        st.rerun()
