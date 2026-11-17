# Archive sections

The IBO archive papers printed their own section names, spelled and organised
differently each year. The archive filter uses eight common sections instead:

Cell Biology · Genetics · Evolution · Animal Anatomy and Physiology ·
Plant Anatomy and Physiology · Ecology · Ethology · Biosystematics

`ibo_questions.section` still holds the name exactly as printed (shown as
"Printed as …" on a question). `ibo_questions.section_group` holds the common
section and is what the filter, the badges and a draft's topic use.

## How a question gets its section

1. **By name**, for papers that print a clear subject: 2013 (Animal, Plant,
   Cell-molecular-microbiology), 2014 (the Russian equivalents) and 2016. Spellings
   and languages that mean the same thing end up in one section.
2. **By content**, listed question by question in `app/archive/section_groups.json`
   (`"2017-A": {"12": "cell-biology", ...}`), for questions that cannot be filed by name:
   - 2015 and 2018 print no sections at all;
   - 2017 uses themed names ("Disease & Decay") that mix subjects;
   - the combined sections "Genetics and evolution" (2013, 2014, 2016) and
     "Ecology, ethology and systematics" (2013) have to be split.

   These were assigned by reading each question, so some are judgement calls.
3. Anything else has no section.

## Changing a question's section

Edit its entry in `section_groups.json`, then clear its `section_group` value
(or set it directly in the database). The value is only filled in where it is
empty, so a section corrected by hand is never overwritten at start-up.

New archive papers get their sections when imported
(`scripts/import_ibo_archive.py`), and the app fills any gaps at start-up.
