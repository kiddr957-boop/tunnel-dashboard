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


def parse_excel(file):
    """
    Parse the lab Excel format:
      Row: Date Received  | date | date | ...
      Row: Client Ref.:   | name | name | ...
      Row: AHK Ref.:      | ref  | ref  | ...
      Row: Material category | Wt % | ...
      Rows: <category>   | val  | val  | ...
      Row: Total          | 100  | 100  | ...
    Returns (dict of {sample_label: {category: wt_pct}}, error_string | None)
    """
    try:
        df_raw = pd.read_excel(file, header=None, dtype=str)
    except Exception as e:
        return None, f"Could not read file: {e}"

    client_refs = []
    data_start = None

    for i, row in df_raw.iterrows():
        cell = str(row.iloc[0]).strip().replace("\xa0", " ")
        # Normalise double-spaces in header labels
        cell_norm = " ".join(cell.split())
        if cell_norm in ("Client Ref.:", "Client Ref:", "Client  Ref.:"):
            client_refs = [
                str(v).strip() for v in row.iloc[1:]
                if pd.notna(v) and str(v).strip() not in ("", "nan")
            ]
        if cell_norm == "Material category":
            data_start = i + 1
            break

    if data_start is None:
        return None, (
            "Could not find a 'Material category' row. "
            "Make sure the file matches the expected lab report format."
        )

    if not client_refs:
        # Fall back to generic column labels if Client Ref row is missing
        n_cols = df_raw.shape[1] - 1
        client_refs = [f"Col {j+1}" for j in range(n_cols)]

    parsed = {ref: {} for ref in client_refs}

    for i in range(data_start, len(df_raw)):
        row = df_raw.iloc[i]
        cat = str(row.iloc[0]).strip()
        if not cat or cat.lower() in ("total", "nan"):
            continue
        if cat not in CATEGORIES:
            continue
        for j, ref in enumerate(client_refs):
            raw_val = row.iloc[j + 1] if j + 1 < len(row) else None
            try:
                parsed[ref][cat] = float(raw_val)
            except (TypeError, ValueError):
                parsed[ref][cat] = 0.0

    return parsed, None


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

sample_tabs = st.tabs(SAMPLES + ["Import Excel", "Compare all"])

# ── S1–S5 manual entry tabs ───────────────────────────────────────────────────

for tab, sample in zip(sample_tabs[:5], SAMPLES):
    with tab:
        existing = existing_values(sample)

        if not all_data.empty:
            row = all_data[all_data["sample_name"] == sample]
            if not row.empty:
                last = row.iloc[0]
                who = last["updated_by"] or "unknown"
                when = pd.to_datetime(last["updated_at"]).strftime("%Y-%m-%d %H:%M")
                st.caption(f"Last saved by **{who}** on {when}")

        user_name = st.text_input(
            "Your name", key=f"user_{sample}", placeholder="Optional"
        )

        with st.form(f"form_{sample}"):
            col_a, col_b = st.columns(2)
            rows = []
            for i, cat in enumerate(CATEGORIES):
                col = col_a if i % 2 == 0 else col_b
                with col:
                    val = st.number_input(
                        cat,
                        min_value=0.0,
                        max_value=100.0,
                        step=0.1,
                        value=float(existing.get(cat, 0.0)),
                        key=f"inp_{sample}_{cat}",
                    )
                rows.append({"category": cat, "wt_pct": val})

            submitted = st.form_submit_button(f"Save {sample}", use_container_width=True)

        if submitted:
            save_sample(sample, user_name.strip(), rows)
            for cat in CATEGORIES:
                st.session_state.pop(f"inp_{sample}_{cat}", None)
            st.success(f"{sample} saved.")
            st.rerun()


# ── Import Excel tab ──────────────────────────────────────────────────────────

with sample_tabs[5]:
    st.subheader("Import from Excel")
    st.caption(
        "Upload the lab report Excel file. The importer looks for a **Client Ref.:** row "
        "to name each column, and a **Material category** row to find the data."
    )

    uploaded = st.file_uploader("Choose .xlsx file", type=["xlsx", "xls"])

    if uploaded:
        parsed, err = parse_excel(uploaded)

        if err:
            st.error(err)
        else:
            st.success(f"Found **{len(parsed)}** sample column(s).")

            # Preview table
            preview_rows = []
            for cat in CATEGORIES:
                row_data = {"Category": cat}
                for ref, vals in parsed.items():
                    row_data[ref] = vals.get(cat, 0.0)
                preview_rows.append(row_data)
            preview_df = pd.DataFrame(preview_rows).set_index("Category")
            st.dataframe(preview_df.style.format("{:.2f}"), use_container_width=True)

            st.markdown("**Map each column to a sample slot:**")
            slot_options = ["— skip —"] + SAMPLES
            mapping = {}
            cols = st.columns(min(len(parsed), 5))
            for i, ref in enumerate(parsed.keys()):
                with cols[i % len(cols)]:
                    default_slot = SAMPLES[i] if i < len(SAMPLES) else "— skip —"
                    chosen = st.selectbox(
                        ref, slot_options,
                        index=slot_options.index(default_slot),
                        key=f"map_{i}",
                    )
                    mapping[ref] = chosen

            if st.button("Import into dashboard", type="primary"):
                imported = []
                for ref, slot in mapping.items():
                    if slot == "— skip —":
                        continue
                    rows = [
                        {"category": cat, "wt_pct": parsed[ref].get(cat, 0.0)}
                        for cat in CATEGORIES
                    ]
                    save_sample(slot, f"Excel import ({ref})", rows)
                    imported.append(f"{ref} → {slot}")
                    # Clear widget state so tab shows new values
                    for cat in CATEGORIES:
                        st.session_state.pop(f"inp_{slot}_{cat}", None)

                if imported:
                    st.success("Imported: " + ", ".join(imported))
                    st.rerun()


# ── Compare all tab ───────────────────────────────────────────────────────────

with sample_tabs[6]:
    if all_data.empty:
        st.info("No data saved yet.")
    else:
        wide = (
            all_data
            .pivot_table(index="category", columns="sample_name", values="wt_pct", aggfunc="last")
            .reindex(CATEGORIES)
        )
        present = [s for s in SAMPLES if s in wide.columns]
        wide = wide[present]
        wide.index.name = "Category"

        totals = wide.sum().rename("TOTAL")
        display = pd.concat([wide, totals.to_frame().T])

        st.dataframe(
            display.style.format("{:.2f}", na_rep="—").highlight_null(color="#2a2a2a"),
            use_container_width=True,
            height=680,
        )

        csv = all_data.to_csv(index=False)
        st.download_button(
            "Download CSV",
            data=csv,
            file_name="feedstock_samples.csv",
            mime="text/csv",
        )
