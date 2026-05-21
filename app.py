import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Feedstock Dashboard", layout="wide")

CATEGORIES = [
    "Paper & Cardboard", "Wood", "Plastic film", "Dense plastics - HDPE/PE",
    "Dense plastics - PET", "Dense plastics - PVC", "Mixed dense plastics",
    "Textiles", "Misc. Combustibles", "Nappies", "Misc. Non Combustibles",
    "Glass", "FE metals", "Non FE metals", "Food Waste", "Garden Waste",
    "Other putrescibles", "WEEE", "Household hazardous", "Fines (<20mm)"
]

SAMPLES = ["S1", "S2", "S3", "S4", "S5"]


@st.cache_resource
def get_engine():
    db = st.secrets["postgresql"]
    url = (
        f"postgresql+psycopg2://{db['user']}:{db['password']}"
        f"@{db['host']}:{db.get('port', 5432)}/{db['dbname']}"
    )
    return create_engine(url, pool_pre_ping=True)


@st.cache_data(ttl=60)
def load_all():
    engine = get_engine()
    q = text("""
        SELECT sample_name, category, wt_pct, updated_by, updated_at
        FROM feedstock_inputs
        WHERE sample_name = ANY(:samples)
        ORDER BY sample_name, category
    """)
    with engine.begin() as conn:
        return pd.read_sql(q, conn, params={"samples": SAMPLES})


def save_sample(sample_name, user_name, rows):
    engine = get_engine()
    df = pd.DataFrame(rows)
    df["sample_name"] = sample_name
    df["updated_by"] = user_name or None
    df["wt_pct"] = df["wt_pct"].astype(float)

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM feedstock_inputs WHERE sample_name = :s"),
            {"s": sample_name},
        )
        conn.execute(
            text("""
                INSERT INTO feedstock_inputs (sample_name, category, wt_pct, updated_by)
                VALUES (:sample_name, :category, :wt_pct, :updated_by)
            """),
            df.to_dict("records"),
        )
    load_all.clear()


# ── Load data ─────────────────────────────────────────────────────────────────

st.title("Feedstock Composition Dashboard")

try:
    all_data = load_all()
except Exception as e:
    st.error(f"Database error: {e}")
    st.stop()


def existing_values(sample):
    if all_data.empty:
        return {}
    mask = all_data["sample_name"] == sample
    return dict(zip(all_data.loc[mask, "category"], all_data.loc[mask, "wt_pct"]))


# ── Tabs ──────────────────────────────────────────────────────────────────────

sample_tabs = st.tabs(SAMPLES + ["Compare all"])

for tab, sample in zip(sample_tabs[:5], SAMPLES):
    with tab:
        existing = existing_values(sample)

        # Last-edited metadata
        if not all_data.empty:
            row = all_data[all_data["sample_name"] == sample]
            if not row.empty:
                last = row.iloc[0]
                who = last["updated_by"] or "unknown"
                when = pd.to_datetime(last["updated_at"]).strftime("%Y-%m-%d %H:%M")
                st.caption(f"Last saved by **{who}** on {when}")

        user_name = st.text_input(
            "Your name", key=f"user_{sample}", placeholder="Optional — shown in history"
        )

        with st.form(f"form_{sample}"):
            col_a, col_b = st.columns(2)
            rows = []
            for i, cat in enumerate(CATEGORIES):
                col = col_a if i % 2 == 0 else col_b
                with col:
                    default = float(existing.get(cat, 0.0))
                    val = st.number_input(
                        cat,
                        min_value=0.0,
                        max_value=100.0,
                        step=0.1,
                        value=default,
                        key=f"inp_{sample}_{cat}",
                    )
                rows.append({"category": cat, "wt_pct": val})

            submitted = st.form_submit_button(f"Save {sample}", use_container_width=True)

        if submitted:
            save_sample(sample, user_name.strip(), rows)
            # Clear widget state so inputs re-initialise from DB on next render
            for cat in CATEGORIES:
                st.session_state.pop(f"inp_{sample}_{cat}", None)
            st.success(f"{sample} saved.")
            st.rerun()


# ── Compare tab ───────────────────────────────────────────────────────────────

with sample_tabs[5]:
    if all_data.empty:
        st.info("No data saved yet — fill in at least one sample to compare.")
    else:
        wide = (
            all_data
            .pivot_table(index="category", columns="sample_name", values="wt_pct", aggfunc="last")
            .reindex(CATEGORIES)
        )
        present = [s for s in SAMPLES if s in wide.columns]
        wide = wide[present]
        wide.index.name = "Category"

        # Totals row
        totals = wide.sum().rename("TOTAL")
        display = pd.concat([wide, totals.to_frame().T])

        st.dataframe(
            display.style.format("{:.1f}", na_rep="—").highlight_null(color="#2a2a2a"),
            use_container_width=True,
            height=660,
        )

        csv = all_data.to_csv(index=False)
        st.download_button(
            "Download CSV",
            data=csv,
            file_name="feedstock_samples.csv",
            mime="text/csv",
        )

        st.caption(
            "Share this dashboard by sending the app URL. "
            "Each sample tab is independent — anyone can edit and save their sample."
        )
