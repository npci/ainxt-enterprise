# PDF Scripts Filling Module

## Brief Introduction

The `pdf_scripts_filling` module is a specialized utility within the legacy Anthropic DocSkills PDF toolkit. It provides two complementary strategies for populating PDF forms with data: a native AcroForm field-filling path for fillable PDFs, and an annotation-based overlay path for non-fillable or image-derived forms. The module is designed to be invoked as a command-line script or imported as a library by higher-level skills, workers, or document-generation pipelines.

This module is part of the broader `docskills_legacy` PDF subsystem. For related capabilities, see the extraction and validation sibling modules.

---

## Module Purpose and Core Functionality

The primary responsibility of `pdf_scripts_filling` is to take a source PDF, a JSON payload describing the desired field values, and produce a filled output PDF. It supports two distinct workflows:

1. **Native Form Field Filling** (`fill_fillable_fields.py`)  
   Uses `pypdf` to update existing AcroForm fields (text, checkbox, radio group, choice/dropdown). It validates the input JSON against the actual field metadata before writing, ensuring page numbers and allowed values are correct.

2. **Annotation-Based Filling** (`fill_pdf_form_with_annotations.py`)  
   For PDFs that do not contain native form fields, this script overlays `FreeText` annotations at coordinates derived from image or PDF coordinate bounding boxes. This is commonly used when forms are detected via OCR or visual layout analysis.

Both scripts are self-contained, accept CLI arguments, and can be embedded in larger document-automation skills.

---

## Core Components

### `fill_fillable_fields.py`

| Component | Responsibility |
|-----------|----------------|
| `fill_pdf_fields(input_pdf_path, fields_json_path, output_pdf_path)` | Loads the field-value JSON, validates it against the PDF's real form fields, groups values by page, and writes the filled PDF using `PdfWriter.update_page_form_field_values`. |
| `validation_error_for_field_value(field_info, field_value)` | Validates a proposed value for a specific field type: checkboxes must match checked/unchecked values, radio groups and choice fields must select from allowed options. |
| `monkeypatch_pydpf_method()` | Patches `pypdf.generic.DictionaryObject.get_inherited` so that choice option arrays returned as `[[export, display], ...]` are flattened to their export values, working around a pypdf parsing quirk. |

### `fill_pdf_form_with_annotations.py`

| Component | Responsibility |
|-----------|----------------|
| `fill_pdf_form(input_pdf_path, fields_json_path, output_pdf_path)` | Reads a JSON description of form fields with bounding boxes and text styling, converts coordinates into PDF space, and adds a `FreeText` annotation for each non-empty entry. |
| `transform_from_image_coords(...)` | Converts a bounding box from image pixel coordinates to PDF page coordinates, flipping the Y axis to match pypdf's coordinate system. |
| `transform_from_pdf_coords(...)` | Converts a bounding box already in PDF coordinates into the pypdf annotation rectangle format. |

---

## Architecture and Component Relationships

```mermaid
graph TB
    subgraph pdf_scripts_filling [PDF Scripts Filling]
        A[fill_fillable_fields.py]
        B[fill_pdf_form_with_annotations.py]
    end

    subgraph pdf_scripts_extraction [PDF Scripts Extraction]
        C[extract_form_field_info.py]
        D[extract_form_structure.py]
    end

    subgraph pdf_scripts_validation [PDF Scripts Validation]
        E[check_bounding_boxes.py]
        F[create_validation_image.py]
    end

    subgraph external [External Libraries]
        G[pypdf PdfReader / PdfWriter]
        H[pypdf.annotations.FreeText]
    end

    A -->|imports get_field_info| C
    A -->|validates against| G
    A -->|writes filled PDF| G
    B -->|reads source PDF| G
    B -->|writes annotated PDF| G
    B -->|uses| H
    C -.->|field metadata JSON| A
    D -.->|form structure JSON| B
    E -.->|validates coordinates| B
    F -.->|visual QA| B
```

### Component Interaction

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Caller / Skill
    participant FFF as fill_fillable_fields.py
    participant EFI as extract_form_field_info.py
    participant FPA as fill_pdf_form_with_annotations.py
    participant PDF as Output PDF

    Caller->>FFF: input.pdf + fields.json + output.pdf
    FFF->>EFI: get_field_info(reader)
    EFI-->>FFF: field metadata (type, page, allowed values)
    FFF->>FFF: validate each field value
    alt Validation fails
        FFF-->>Caller: print errors and exit(1)
    else Validation passes
        FFF->>PDF: update_page_form_field_values
        FFF-->>Caller: filled PDF
    end

    Caller->>FPA: input.pdf + fields.json + output.pdf
    FPA->>FPA: read pages & dimensions
    FPA->>FPA: transform bounding boxes
    FPA->>PDF: add FreeText annotations
    FPA-->>Caller: annotated PDF
```

---

## Data Flow

### Native Field Filling Flow

```mermaid
flowchart LR
    A[Input PDF with AcroForm] --> B[Load fields.json]
    B --> C[Call get_field_info]
    C --> D[Validate field IDs, pages, and values]
    D -->|Invalid| E[Print errors & exit 1]
    D -->|Valid| F[Group values by page]
    F --> G[Update form field values]
    G --> H[Set NeedAppearances]
    H --> I[Write output PDF]
```

### Annotation-Based Filling Flow

```mermaid
flowchart LR
    A[Input PDF] --> B[Load fields.json]
    B --> C[Read page dimensions]
    C --> D[For each form field]
    D --> E{Has entry_text?}
    E -->|No| D
    E -->|Yes| F[Transform bounding box]
    F --> G[Create FreeText annotation]
    G --> H[Add annotation to page]
    H --> D
    D --> I[Write output PDF]
```

---

## Input / Output Contracts

### Native Filling JSON (`fields.json`)

An array of field entries. Only fields containing a `"value"` key are filled.

```json
[
  {
    "field_id": "name.first",
    "page": 1,
    "value": "Alice"
  },
  {
    "field_id": "consent",
    "page": 1,
    "value": "Yes"
  }
]
```

Validation rules:

- `field_id` must exist in the PDF.
- `page` must match the page recorded in the field metadata.
- Checkbox values must equal the recorded `checked_value` or `unchecked_value`.
- Radio group and choice values must be one of the allowed options.

### Annotation Filling JSON (`fields.json`)

A document describing pages and detected form fields with bounding boxes.

```json
{
  "pages": [
    {
      "page_number": 1,
      "image_width": 612,
      "image_height": 792
    }
  ],
  "form_fields": [
    {
      "page_number": 1,
      "entry_bounding_box": [100, 200, 300, 220],
      "entry_text": {
        "text": "Alice",
        "font": "Arial",
        "font_size": 14,
        "font_color": "000000"
      }
    }
  ]
}
```

If a page object contains `pdf_width`, coordinates are assumed to be in PDF space; otherwise they are treated as image pixel coordinates.

---

## How It Fits into the Overall System

`pdf_scripts_filling` sits at the bottom of the document-generation stack. It is consumed by:

- **Higher-level document skills** in `shared_skills` and `docskills_legacy` that generate or complete PDF forms.
- **Agent and workflow workers** that need to produce filled government, legal, or HR forms as deliverables.
- **Validation scripts** in `pdf_scripts_validation` that verify the output visually or programmatically.

The module is intentionally low-level: it does not decide what values to write, nor does it perform OCR or layout detection. Those responsibilities live in the extraction and AI planning layers. This module's job is to reliably and safely materialize a data payload into a PDF.

```mermaid
graph TB
    subgraph upstream [Upstream Planning / AI Layers]
        U1[Agent / Workflow]
        U2[Document Generation Skill]
        U3[OCR / Layout Analysis]
    end

    subgraph pdf_layer [PDF Toolkit]
        E[pdf_scripts_extraction]
        F[pdf_scripts_filling]
        V[pdf_scripts_validation]
    end

    subgraph downstream [Downstream Consumers]
        D1[File Storage]
        D2[User Download]
        D3[Further Processing]
    end

    U1 -->|decides values| U2
    U3 -->|provides coordinates| U2
    U2 -->|fields.json| F
    E -->|field metadata| F
    F -->|filled PDF| V
    F -->|filled PDF| D1
    F -->|filled PDF| D2
    F -->|filled PDF| D3
```

---

## Process Flow: End-to-End Form Fill

```mermaid
flowchart TB
    A[User / Agent requests form fill] --> B{Does PDF have native AcroForm fields?}
    B -->|Yes| C[Run extract_form_field_info.py]
    C --> D[Generate or receive fields.json]
    D --> E[Run fill_fillable_fields.py]
    E --> F[Validate & fill native fields]
    B -->|No / image-based| G[Run extract_form_structure.py or OCR]
    G --> H[Generate fields.json with bounding boxes]
    H --> I[Run fill_pdf_form_with_annotations.py]
    I --> J[Overlay FreeText annotations]
    F --> K[Output PDF]
    J --> K
    K --> L[Optional: run validation scripts]
```

---

## Error Handling and Safety

- **Native filling** performs strict pre-flight validation and exits with code `1` if any field is unknown, on the wrong page, or has an illegal value. This prevents producing silently broken forms.
- **Annotation filling** skips fields that lack `entry_text` or contain empty text, ensuring only meaningful annotations are added.
- The `monkeypatch_pydpf_method` workaround is applied at CLI entry time to normalize pypdf's handling of choice option arrays, preventing dropdown fills from failing on certain PDFs.

---

## References

- [pdf_scripts_extraction.md](pdf_scripts_extraction.md) — Field metadata and form structure extraction.
- [pdf_scripts_validation.md](pdf_scripts_validation.md) — Bounding-box and visual validation of filled PDFs.
- [pdf_skills_filling.md](pdf_skills_filling.md) — The ABStudio/Anthropic PDF filling counterpart.
- [docskills_legacy.md](docskills_legacy.md) — Overview of the legacy DocSkills document toolkit.
- [shared_skills.md](shared_skills.md) — Broader skill ecosystem including docx, pptx, xlsx, and PDF skills.
