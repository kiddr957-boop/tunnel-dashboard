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

@st.cache_resource
def get_engine():
    db = st.secrets["postgresql"]
    url = f"postgresql+psycopg2://{db['user']}:{db['password']}@{db['host']}:{db.get('port', 5432)}/{db['dbname']}"
    return create_engine(url, pool_pre_ping=True)

@st.cache_data(ttl=120)
def load_samples():
    engine = get_engine()
    q = text("""
        SELECT sample_name, category, wt_pct, updated_by, updated_at
        FROM feedstock_inputs
        ORDER BY updated_at DESC, sample_name, category
    """)
    with engine.begin() as conn:
        return pd.read_sql(q, conn)

def save_sample(sample_name, user_name, rows):
    engine = get_engine()
    df = pd.DataFrame(rows)
    df = df[df["wt_pct"].notna()].copy()
    df["sample_name"] = sample_name
    df["updated_by"] = user_name
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
    load_samples.clear()

st.title("Feedstock Composition Dashboard")
st.caption("Editable Streamlit dashboard backed by PostgreSQL.")

left, right = st.columns([1, 2])

with left:
    st.subheader("Edit sample")
    sample_name = st.text_input("Sample name", value="Sample 1 MSW")
    user_name = st.text_input("Your name", value="")

    rows = []
    with st.form("feedstock_form"):
        for cat in CATEGORIES:
            val = st.number_input(cat, min_value=0.0, max_value=100.0, step=0.1, value=0.0)
            rows.append({"category": cat, "wt_pct": val})
        submitted = st.form_submit_button("Save sample")

    if submitted:
        if not sample_name.strip():
            st.error("Enter a sample name.")
        else:
            save_sample(sample_name.strip(), user_name.strip() or None, rows)
            st.success("Saved to PostgreSQL.")

with right:
    st.subheader("Saved records")
    try:
        data = load_samples()
        if data.empty:
            st.info("No records saved yet.")
        else:
            st.dataframe(data, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Database error: {e}")
