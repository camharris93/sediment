{#
  Use the model's configured `+schema` verbatim (raw → staging → marts) instead
  of dbt's default `<target_schema>_<custom_schema>` concatenation. This is what
  gives us clean `staging` and `marts` schemas inside the single DuckDB file,
  matching the PRD's raw/staging/marts layout.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
