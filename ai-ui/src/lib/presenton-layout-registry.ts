// SPDX-License-Identifier: Apache-2.0
/**
 * presenton-layout-registry.ts
 *
 * Static registry of all local presentation template layouts.
 * Each entry maps to a slide layout file in src/presentation-templates/
 * and provides the JSON Schema needed for the Presenton /prepare API payload.
 *
 * Format per slide entry:
 *   id          – "{group}:{layoutId}"
 *   name        – human-readable layout name
 *   description – layout description
 *   json_schema – JSON Schema Draft 2020-12 object derived from the Zod schema
 *   templateID  – group name (e.g. "general")
 *   templateName– group name (same as templateID)
 */

export interface SlideLayoutEntry {
  id: string;
  name: string;
  description: string;
  json_schema: Record<string, unknown>;
  templateID: string;
  templateName: string;
}

export interface LayoutGroup {
  name: string;
  ordered: boolean;
  slides: SlideLayoutEntry[];
}

// ─── Shared sub-schemas ────────────────────────────────────────────────────────

const IMAGE_SCHEMA = {
  type: 'object',
  properties: {
    __image_url__: { type: 'string', format: 'uri', description: 'URL to image' },
    __image_prompt__: { type: 'string', minLength: 10, maxLength: 120, description: 'Prompt used to generate the image' },
  },
  required: ['__image_url__', '__image_prompt__'],
  additionalProperties: false,
};

const ICON_SCHEMA = {
  type: 'object',
  properties: {
    __icon_url__: { type: 'string', description: 'URL to icon' },
    __icon_query__: { type: 'string', minLength: 5, maxLength: 40, description: 'Query used to search the icon' },
  },
  required: ['__icon_url__', '__icon_query__'],
  additionalProperties: false,
};

function jsonSchema(properties: Record<string, unknown>, required: string[]): Record<string, unknown> {
  return {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties,
    required,
    additionalProperties: false,
  };
}

// ─── general group ─────────────────────────────────────────────────────────────

const GENERAL_SLIDES: SlideLayoutEntry[] = [
  {
    id: 'general:general-intro-slide',
    name: 'Intro Slide',
    description: 'A clean slide layout with title, description text, presenter info, and a supporting image.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Main description text content' },
      presenterName: { type: 'string', minLength: 2, maxLength: 50, description: 'Name of the presenter' },
      presentationDate: { type: 'string', minLength: 2, maxLength: 50, description: "Date of the presentation must be the latest date like today's date" },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
    }, ['title', 'description', 'presenterName', 'presentationDate', 'image']),
  },
  {
    id: 'general:basic-info-slide',
    name: 'Basic Info',
    description: 'A clean slide layout with title, description text, and a supporting image.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Main description text content' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
    }, ['title', 'description', 'image']),
  },
  {
    id: 'general:bullet-icons-only-slide',
    name: 'Bullet Icons Only',
    description: 'A slide layout with title, grid of bullet points (title and description) with icons, and a supporting image.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
      bulletPoints: {
        type: 'array',
        minItems: 2,
        maxItems: 3,
        description: 'List of bullet points with icons and optional subtitles',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 80, description: 'Bullet point title' },
            subtitle: { type: 'string', minLength: 5, maxLength: 150, description: 'Optional short subtitle or brief explanation' },
            icon: ICON_SCHEMA,
          },
          required: ['title', 'icon'],
          additionalProperties: false,
        },
      },
    }, ['title', 'image', 'bulletPoints']),
  },
  {
    id: 'general:bullet-with-icons-slide',
    name: 'Bullet with Icons',
    description: 'A bullets style slide with main content, supporting image, and bullet points with icons and descriptions.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', maxLength: 150, description: 'Main description text explaining the problem or topic' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
      bulletPoints: {
        type: 'array',
        minItems: 1,
        maxItems: 3,
        description: 'List of bullet points with icons and descriptions',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 60, description: 'Bullet point title' },
            description: { type: 'string', minLength: 10, maxLength: 100, description: 'Bullet point description' },
            icon: ICON_SCHEMA,
          },
          required: ['title', 'description', 'icon'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'image', 'bulletPoints']),
  },
  {
    id: 'general:chart-with-bullets-slide',
    name: 'Chart with Bullet Boxes',
    description: 'A slide layout with title, description, chart on the left and colored bullet boxes with icons on the right. Only choose this if data is available.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      chartData: {
        oneOf: [
          {
            type: 'object',
            properties: {
              type: { type: 'string', enum: ['bar', 'pie', 'line', 'area'] },
              data: {
                type: 'array', minItems: 2, maxItems: 5,
                items: {
                  type: 'object',
                  properties: {
                    name: { type: 'string', description: 'Data point name' },
                    value: { type: 'number', description: 'Data point value' },
                  },
                  required: ['name', 'value'],
                },
              },
            },
            required: ['type', 'data'],
          },
          {
            type: 'object',
            properties: {
              type: { type: 'string', enum: ['scatter'] },
              data: {
                type: 'array', minItems: 2, maxItems: 20,
                items: {
                  type: 'object',
                  properties: {
                    x: { type: 'number', description: 'X coordinate' },
                    y: { type: 'number', description: 'Y coordinate' },
                  },
                  required: ['x', 'y'],
                },
              },
            },
            required: ['type', 'data'],
          },
        ],
        description: 'Chart data with type and data points',
      },
      color: { type: 'string', description: 'Primary color for chart elements' },
      showLegend: { type: 'boolean', description: 'Whether to show chart legend' },
      showTooltip: { type: 'boolean', description: 'Whether to show chart tooltip' },
      bulletPoints: {
        type: 'array',
        minItems: 1,
        maxItems: 3,
        description: 'List of bullet points with colored boxes and icons',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 80, description: 'Bullet point title' },
            description: { type: 'string', minLength: 10, maxLength: 150, description: 'Bullet point description' },
            icon: ICON_SCHEMA,
          },
          required: ['title', 'description', 'icon'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'chartData', 'bulletPoints']),
  },
  {
    id: 'general:metrics-slide',
    name: 'Metrics',
    description: 'A slide layout with title, description, and a grid of metric cards with values and labels.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      metrics: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'List of metric cards',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 20, description: 'Metric value (e.g. "$2.4M", "15%")' },
            label: { type: 'string', minLength: 2, maxLength: 50, description: 'Metric label' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Short description of the metric' },
            icon: ICON_SCHEMA,
          },
          required: ['value', 'label', 'icon'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'metrics']),
  },
  {
    id: 'general:metrics-with-image-slide',
    name: 'Metrics with Image',
    description: 'A slide layout with title, description, metrics, and a supporting image.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
      metrics: {
        type: 'array',
        minItems: 2,
        maxItems: 3,
        description: 'List of metric cards',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 20, description: 'Metric value' },
            label: { type: 'string', minLength: 2, maxLength: 50, description: 'Metric label' },
            icon: ICON_SCHEMA,
          },
          required: ['value', 'label', 'icon'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'image', 'metrics']),
  },
  {
    id: 'general:numbered-bullets-slide',
    name: 'Numbered Bullets',
    description: 'A slide layout with title, description, and numbered bullet points.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
      bulletPoints: {
        type: 'array',
        minItems: 2,
        maxItems: 5,
        description: 'List of numbered bullet points',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 80, description: 'Bullet point title' },
            description: { type: 'string', minLength: 5, maxLength: 150, description: 'Bullet point description' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'image', 'bulletPoints']),
  },
  {
    id: 'general:quote-slide',
    name: 'Quote',
    description: 'A slide layout with a prominent quote, author attribution, and supporting image.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      quote: { type: 'string', minLength: 10, maxLength: 300, description: 'The main quote text' },
      author: { type: 'string', minLength: 2, maxLength: 80, description: 'Quote author name' },
      authorTitle: { type: 'string', minLength: 2, maxLength: 80, description: 'Author title or role' },
      image: { ...IMAGE_SCHEMA, description: 'Supporting image for the slide' },
    }, ['quote', 'author', 'image']),
  },
  {
    id: 'general:table-info-slide',
    name: 'Table Info',
    description: 'A slide layout with title, description, and a data table.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      tableData: {
        type: 'object',
        properties: {
          headers: { type: 'array', items: { type: 'string' }, minItems: 2, maxItems: 6, description: 'Table column headers' },
          rows: {
            type: 'array',
            minItems: 1,
            maxItems: 8,
            items: { type: 'array', items: { type: 'string' }, description: 'Table row cells' },
            description: 'Table rows',
          },
        },
        required: ['headers', 'rows'],
        description: 'Table data with headers and rows',
      },
    }, ['title', 'description', 'tableData']),
  },
  {
    id: 'general:table-of-contents-slide',
    name: 'Table of Contents',
    description: 'A slide layout showing a table of contents with numbered sections.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      items: {
        type: 'array',
        minItems: 2,
        maxItems: 8,
        description: 'Table of contents items',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 80, description: 'Section title' },
            description: { type: 'string', minLength: 5, maxLength: 150, description: 'Section description' },
          },
          required: ['title'],
          additionalProperties: false,
        },
      },
    }, ['title', 'items']),
  },
  {
    id: 'general:team-slide',
    name: 'Team',
    description: 'A slide layout showcasing team members with photos, names, and roles.',
    templateID: 'general',
    templateName: 'general',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 150, description: 'Description text below the title' },
      teamMembers: {
        type: 'array',
        minItems: 1,
        maxItems: 4,
        description: 'List of team members',
        items: {
          type: 'object',
          properties: {
            name: { type: 'string', minLength: 2, maxLength: 50, description: 'Team member name' },
            role: { type: 'string', minLength: 2, maxLength: 80, description: 'Team member role or title' },
            bio: { type: 'string', minLength: 10, maxLength: 200, description: 'Short bio' },
            image: IMAGE_SCHEMA,
          },
          required: ['name', 'role', 'image'],
          additionalProperties: false,
        },
      },
    }, ['title', 'teamMembers']),
  },
];

// ─── Modern template slides (COMMENTED OUT) ────────────────────────────────────
/*
export const MODERN_SLIDES: SlideLayoutEntry[] = [
  {
    id: 'modern:intro-slide',
    name: 'Intro Pitch Deck Slide',
    description: 'A visually appealing introduction slide for a pitch deck, featuring a large title, company name, date, and contact information with a modern design.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 2, maxLength: 15, description: 'Main title of the slide' },
      description: { type: 'string', default: '', description: 'Description as per the design' },
      contactNumber: { type: 'string', minLength: 5, maxLength: 20, description: 'Contact phone number displayed in footer' },
      contactAddress: { type: 'string', minLength: 10, maxLength: 100, description: 'Contact address displayed in footer' },
      contactWebsite: { type: 'string', minLength: 5, maxLength: 60, description: 'Contact website URL displayed in footer' },
      companyName: { type: 'string', minLength: 2, maxLength: 50, description: 'Company name displayed in header' },
      date: { type: 'string', minLength: 5, maxLength: 50, description: 'Date of the presentation' },
    }, ['title', 'companyName', 'date']),
  },
  {
    id: 'modern:about-company-slide',
    name: 'About Our Company Slide',
    description: 'A slide layout providing an overview of the company, its background, and key information.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 30, description: 'Main title of the slide' },
      content: { type: 'string', minLength: 25, maxLength: 400, description: 'Main content text describing the company or topic' },
      companyName: { type: 'string', minLength: 2, maxLength: 50, description: 'Company name displayed in header' },
      date: { type: 'string', minLength: 5, maxLength: 30, description: 'Today Date displayed in header' },
      image: { ...IMAGE_SCHEMA, description: 'Optional supporting image for the slide (building, office, etc.)' },
    }, ['title', 'content', 'companyName', 'date']),
  },
  {
    id: 'modern:problem-slide',
    name: 'Problem Statement Slide',
    description: 'A slide layout designed to present a clear problem statement, including categories of problems, company information, and an optional image.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 20, description: 'Main title of the problem statement slide' },
      description: { type: 'string', minLength: 50, maxLength: 200, description: 'Main content text describing the problem statement' },
      problemCategories: {
        type: 'array',
        minItems: 2,
        maxItems: 3,
        description: 'List of problem categories with titles, descriptions, and optional icons',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 3, maxLength: 30, description: 'Title of the problem category' },
            description: { type: 'string', minLength: 20, maxLength: 100, description: 'Description of the problem category' },
            icon: { ...ICON_SCHEMA, description: 'Optional icon for the problem category' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
      companyName: { type: 'string', minLength: 2, maxLength: 50, description: 'Company name displayed in header' },
      date: { type: 'string', minLength: 5, maxLength: 30, description: 'Today Date displayed in header' },
    }, ['title', 'description', 'problemCategories', 'companyName', 'date']),
  },
  {
    id: 'modern:solution-slide',
    name: 'Solution Slide',
    description: 'A slide to present the solution or product offering.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Solution description' },
      features: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Key features or benefits',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 40, description: 'Feature title' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Feature description' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'features']),
  },
  {
    id: 'modern:product-overview-slide',
    name: 'Product Overview Slide',
    description: 'A slide showcasing the product with features and benefits.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Product description' },
      keyPoints: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Key product points',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 40, description: 'Point title' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Point description' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'keyPoints']),
  },
  {
    id: 'modern:market-size-slide',
    name: 'Market Size Slide',
    description: 'A slide presenting market size, TAM, SAM, SOM data.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Market description' },
      marketData: {
        type: 'object',
        properties: {
          tam: { type: 'string', minLength: 1, maxLength: 30, description: 'Total Addressable Market' },
          sam: { type: 'string', minLength: 1, maxLength: 30, description: 'Serviceable Addressable Market' },
          som: { type: 'string', minLength: 1, maxLength: 30, description: 'Serviceable Obtainable Market' },
        },
        required: ['tam', 'sam', 'som'],
        additionalProperties: false,
      },
    }, ['title', 'description', 'marketData']),
  },
  {
    id: 'modern:market-validation-slide',
    name: 'Market Validation Slide',
    description: 'A slide showing market validation and traction metrics.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Validation description' },
      metrics: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Key validation metrics',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 20, description: 'Metric value' },
            label: { type: 'string', minLength: 2, maxLength: 50, description: 'Metric label' },
          },
          required: ['value', 'label'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'metrics']),
  },
  {
    id: 'modern:company-traction-slide',
    name: 'Company Traction Slide',
    description: 'A slide showcasing company traction and growth metrics.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Traction description' },
      milestones: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Key milestones achieved',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 40, description: 'Milestone title' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Milestone description' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'milestones']),
  },
  {
    id: 'modern:business-model-slide',
    name: 'Business Model Slide',
    description: 'A slide explaining the business model and revenue streams.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Business model description' },
      revenueStreams: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Revenue streams',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 2, maxLength: 40, description: 'Stream title' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Stream description' },
          },
          required: ['title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'revenueStreams']),
  },
  {
    id: 'modern:team-slide',
    name: 'Team Slide',
    description: 'A slide introducing the team members.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Team description' },
      members: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Team members',
        items: {
          type: 'object',
          properties: {
            name: { type: 'string', minLength: 2, maxLength: 50, description: 'Member name' },
            role: { type: 'string', minLength: 2, maxLength: 50, description: 'Member role' },
            bio: { type: 'string', minLength: 5, maxLength: 150, description: 'Member bio' },
          },
          required: ['name', 'role'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'members']),
  },
  {
    id: 'modern:thank-you-slide',
    name: 'Thank You Slide',
    description: 'A closing slide with thank you message and contact information.',
    templateID: 'modern',
    templateName: 'modern',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title of the slide' },
      message: { type: 'string', minLength: 10, maxLength: 300, description: 'Thank you message' },
      contactEmail: { type: 'string', minLength: 5, maxLength: 100, description: 'Contact email' },
      contactPhone: { type: 'string', minLength: 5, maxLength: 30, description: 'Contact phone' },
    }, ['title', 'message']),
  },
];
*/

// ─── Standard template slides (COMMENTED OUT) ──────────────────────────────────
/*
export const STANDARD_SLIDES: SlideLayoutEntry[] = [
  {
    id: 'standard:intro-slide',
    name: 'Intro Slide',
    description: 'A slide with header, title, subtitle, body text, and image. Perfect for title/cover slides.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      subtitle: { type: 'string', minLength: 3, maxLength: 100, description: 'Subtitle' },
      body: { type: 'string', minLength: 10, maxLength: 500, description: 'Body text' },
      image: {
        type: 'object',
        properties: {
          __image_url__: { type: 'string', format: 'uri', description: 'URL to image' },
          __image_prompt__: { type: 'string', minLength: 10, maxLength: 150, description: 'Prompt used to generate the image' },
        },
        required: ['__image_url__', '__image_prompt__'],
        additionalProperties: false,
      },
    }, ['title']),
  },
  {
    id: 'standard:table-of-contents',
    name: 'Table of Contents',
    description: 'A slide showing the table of contents with numbered items.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Title' },
      items: {
        type: 'array',
        minItems: 3,
        maxItems: 10,
        description: 'TOC items',
        items: {
          type: 'object',
          properties: {
            number: { type: 'string', minLength: 1, maxLength: 3, description: 'Item number' },
            title: { type: 'string', minLength: 2, maxLength: 60, description: 'Item title' },
          },
          required: ['number', 'title'],
          additionalProperties: false,
        },
      },
    }, ['title', 'items']),
  },
  {
    id: 'standard:heading-bullet-image',
    name: 'Heading with Bullet Points and Image',
    description: 'A slide with heading, bullet points, and supporting image.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      bullets: {
        type: 'array',
        minItems: 2,
        maxItems: 5,
        description: 'Bullet points',
        items: {
          type: 'object',
          properties: {
            text: { type: 'string', minLength: 5, maxLength: 150, description: 'Bullet text' },
          },
          required: ['text'],
          additionalProperties: false,
        },
      },
      image: {
        type: 'object',
        properties: {
          __image_url__: { type: 'string', format: 'uri', description: 'URL to image' },
          __image_prompt__: { type: 'string', minLength: 10, maxLength: 150, description: 'Prompt used to generate the image' },
        },
        required: ['__image_url__', '__image_prompt__'],
        additionalProperties: false,
      },
    }, ['title', 'bullets']),
  },
  {
    id: 'standard:icon-bullets',
    name: 'Icon Bullets Description',
    description: 'A slide with icons, titles, and descriptions in a grid layout.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      items: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Items with icons',
        items: {
          type: 'object',
          properties: {
            icon: {
              type: 'object',
              properties: {
                __icon_url__: { type: 'string', description: 'URL to icon' },
                __icon_query__: { type: 'string', minLength: 3, maxLength: 40, description: 'Query used to search the icon' },
              },
              required: ['__icon_url__', '__icon_query__'],
              additionalProperties: false,
            },
            title: { type: 'string', minLength: 2, maxLength: 40, description: 'Item title' },
            description: { type: 'string', minLength: 5, maxLength: 150, description: 'Item description' },
          },
          required: ['icon', 'title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['title', 'items']),
  },
  {
    id: 'standard:metrics-description',
    name: 'Metrics with Description',
    description: 'A slide showing key metrics with descriptions.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      metrics: {
        type: 'array',
        minItems: 2,
        maxItems: 4,
        description: 'Key metrics',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 20, description: 'Metric value' },
            label: { type: 'string', minLength: 2, maxLength: 50, description: 'Metric label' },
            description: { type: 'string', minLength: 5, maxLength: 100, description: 'Metric description' },
          },
          required: ['value', 'label'],
          additionalProperties: false,
        },
      },
    }, ['title', 'metrics']),
  },
  {
    id: 'standard:chart-left-text-right',
    name: 'Chart Left Text Right',
    description: 'Standard layout with chart on the left and text on the right.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      description: { type: 'string', minLength: 10, maxLength: 300, description: 'Description text' },
      chartType: { type: 'string', enum: ['bar', 'line', 'pie'], description: 'Type of chart' },
      chartData: {
        type: 'object',
        properties: {
          labels: { type: 'array', items: { type: 'string' }, description: 'Chart labels' },
          values: { type: 'array', items: { type: 'number' }, description: 'Chart values' },
        },
        required: ['labels', 'values'],
        additionalProperties: false,
      },
    }, ['title', 'description']),
  },
  {
    id: 'standard:chart-left-text-right-layout',
    name: 'Chart Left Text Right',
    description: 'A slide with header label, a left-side inline bar chart, and right-side title with paragraph.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 16, maxLength: 64, description: 'Main heading (max ~7 words)' },
      paragraph: { type: 'string', minLength: 50, maxLength: 200, description: 'Supporting description' },
      chart: {
        type: 'object',
        properties: {
          type: { type: 'string', enum: ['bar', 'horizontalBar', 'line', 'pie'], description: 'Chart type' },
          data: {
            type: 'array',
            minItems: 3,
            maxItems: 8,
            description: 'Chart data points',
            items: {
              type: 'object',
              properties: {
                label: { type: 'string', minLength: 1, maxLength: 12, description: 'Category label' },
                value: { type: 'number', minimum: 0, maximum: 100, description: 'Value 0-100' },
              },
              required: ['label', 'value'],
              additionalProperties: false,
            },
          },
          primaryColor: { type: 'string', description: 'Primary color for chart' },
          gridColor: { type: 'string', description: 'Grid color for chart' },
          pieColors: { type: 'array', items: { type: 'string' }, description: 'Colors for pie chart segments' },
          showLabels: { type: 'boolean', description: 'Show labels on chart' },
        },
        required: ['type', 'data', 'primaryColor', 'gridColor', 'pieColors', 'showLabels'],
        additionalProperties: false,
      },
    }, ['title', 'paragraph', 'chart']),
  },
  {
    id: 'standard:icon-bullet-description',
    name: 'Icon Bullet Description',
    description: 'A slide with a small header label and number, a left card of cards with round symbols and titles with descriptions, a large heading with supporting text.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      headerNumber: { type: 'string', minLength: 1, maxLength: 3, description: 'Small header number text. Max 3 characters' },
      rightTitle: { type: 'string', minLength: 24, maxLength: 72, description: 'Large heading on the right. Max 8 words' },
      rightDescription: { type: 'string', minLength: 120, maxLength: 240, description: 'Supporting paragraph under the large heading. Max 40 words' },
      cards: {
        type: 'array',
        minItems: 1,
        maxItems: 4,
        description: 'Array of cards with a round symbol, title and description. Max 6 items',
        items: {
          type: 'object',
          properties: {
            symbolText: { type: 'string', minLength: 1, maxLength: 1, description: 'Single-character symbol inside the round badge' },
            symbolIcon: { ...ICON_SCHEMA, description: 'Optional icon representation for the round symbol' },
            title: { type: 'string', minLength: 16, maxLength: 38, description: 'Title for the card item. Max 4 words' },
            description: { type: 'string', minLength: 50, maxLength: 100, description: 'Description for the card item. Max 15 words.' },
          },
          required: ['symbolText', 'title', 'description'],
          additionalProperties: false,
        },
      },
    }, ['headerNumber', 'rightTitle', 'rightDescription', 'cards']),
  },
  {
    id: 'standard:standard-visual-metrics',
    name: 'Visual Metrics',
    description: 'Standard layout with visual metrics and supporting content.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 40, description: 'Slide title' },
      description: { type: 'string', minLength: 10, maxLength: 200, description: 'Main description' },
      metrics: {
        type: 'array',
        minItems: 2,
        maxItems: 6,
        description: 'Visual metrics',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 20, description: 'Metric value' },
            label: { type: 'string', minLength: 2, maxLength: 50, description: 'Metric label' },
            icon: ICON_SCHEMA,
          },
          required: ['value', 'label'],
          additionalProperties: false,
        },
      },
    }, ['title', 'description', 'metrics']),
  },
  {
    id: 'standard:contact',
    name: 'Contact Slide',
    description: 'A slide with contact information and social links.',
    templateID: 'standard',
    templateName: 'standard',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      email: { type: 'string', minLength: 5, maxLength: 100, description: 'Email address' },
      phone: { type: 'string', minLength: 5, maxLength: 30, description: 'Phone number' },
      address: { type: 'string', minLength: 5, maxLength: 200, description: 'Physical address' },
      website: { type: 'string', minLength: 5, maxLength: 100, description: 'Website URL' },
    }, ['title']),
  },
];
*/

// ─── Swift template slides ─────────────────────────────────────────────────────

export const SWIFT_SLIDES: SlideLayoutEntry[] = [
  {
    id: 'swift:intro-slide',
    name: 'Intro Slide',
    description: 'Intro slide with header, title, subtitle, body, image. If used for last slide, then intro card should be disabled.',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 12, maxLength: 68, description: 'Main slide title' },
      subtitlePrefix: { type: 'string', minLength: 3, maxLength: 40, description: 'First part of subtitle' },
      subtitleAccent: { type: 'string', minLength: 3, maxLength: 40, description: 'Second part of subtitle' },
      paragraph: { type: 'string', minLength: 40, maxLength: 200, description: 'Body paragraph text' },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' },
      introCard: {
        type: 'object',
        properties: {
          enabled: { type: 'boolean', description: 'Show intro card with name and date' },
          name: { type: 'string', minLength: 3, maxLength: 40, description: 'Display name' },
          date: { type: 'string', minLength: 4, maxLength: 40, description: 'Display date' }
        },
        required: ['enabled', 'name', 'date'],
        additionalProperties: false
      },
      media: {
        type: 'object',
        properties: {
          type: { type: 'string', enum: ['image'], description: 'Media type' },
          image: {
            type: 'object',
            properties: {
              __image_url__: { type: 'string', format: 'uri', description: 'URL to image' },
              __image_prompt__: { type: 'string', minLength: 0, maxLength: 120, description: 'Prompt used to generate the image' }
            },
            required: ['__image_url__', '__image_prompt__'],
            additionalProperties: false
          }
        },
        required: ['type', 'image'],
        additionalProperties: false
      }
    }, ['title', 'subtitlePrefix', 'subtitleAccent', 'paragraph', 'website', 'introCard', 'media']),
  },
  {
    id: 'swift:table-of-contents',
    name: 'Table of Contents',
    description: 'Swift: Table of contents with up to 10 items (title + description)',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Title' },
      items: {
        type: 'array',
        minItems: 1,
        maxItems: 10,
        description: 'TOC items with title and description',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 3, maxLength: 40, description: 'Item title' },
            description: { type: 'string', minLength: 0, maxLength: 60, description: 'Item description' }
          },
          required: ['title', 'description'],
          additionalProperties: false
        }
      },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' }
    }, ['title', 'items', 'website']),
  },
  {
    id: 'swift:simple-bullet-points-layout',
    name: 'Simple Bullets',
    description: 'Swift: Simple bullet points with title, statement, and points (each with title and body)',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 4, maxLength: 36, description: 'Main title' },
      statement: { type: 'string', minLength: 20, maxLength: 260, description: 'Opening statement paragraph' },
      points: {
        type: 'array',
        minItems: 1,
        maxItems: 4,
        description: 'Bullet points with titles and bodies',
        items: {
          type: 'object',
          properties: {
            title: { type: 'string', minLength: 6, maxLength: 60, description: 'Point title' },
            body: { type: 'string', minLength: 30, maxLength: 220, description: 'Point body text' }
          },
          required: ['title', 'body'],
          additionalProperties: false
        }
      },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' }
    }, ['title', 'statement', 'points', 'website']),
  },
  {
    id: 'swift:bullets-with-icons-title-description',
    name: 'Bullets with Icons Title Description',
    description: 'Bullet with icons with title and description and title and description for whole',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 3, maxLength: 60, description: 'Main title' },
      sideHeading: { type: 'string', minLength: 0, maxLength: 60, description: 'Side heading text' },
      sideParagraph: { type: 'string', minLength: 0, maxLength: 300, description: 'Side paragraph text' },
      items: {
        type: 'array',
        minItems: 3,
        maxItems: 4,
        description: 'Items with icons, titles, and descriptions',
        items: {
          type: 'object',
          properties: {
            icon: {
              type: 'object',
              properties: {
                __icon_url__: { type: 'string', description: 'URL to icon' },
                __icon_query__: { type: 'string', minLength: 0, maxLength: 80, description: 'Query used to search the icon' }
              },
              required: ['__icon_url__', '__icon_query__'],
              additionalProperties: false
            },
            title: { type: 'string', minLength: 3, maxLength: 40, description: 'Item title' },
            description: { type: 'string', minLength: 0, maxLength: 160, description: 'Item description' }
          },
          required: ['icon', 'title', 'description'],
          additionalProperties: false
        }
      },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' }
    }, ['title', 'sideHeading', 'sideParagraph', 'items', 'website']),
  },
  {
    id: 'swift:metrics-numbers',
    name: 'Metrics Numbers',
    description: 'Swift: Our Impact in Numbers with three stacked metric cards',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 8, maxLength: 60, description: 'Main title' },
      leftTitle: { type: 'string', minLength: 6, maxLength: 40, description: 'Left section title (supports line breaks)' },
      leftBody: { type: 'string', minLength: 30, maxLength: 220, description: 'Left section body text' },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' },
      metrics: {
        type: 'array',
        minItems: 1,
        maxItems: 4,
        description: 'Metric cards with value, lines, and description',
        items: {
          type: 'object',
          properties: {
            value: { type: 'string', minLength: 1, maxLength: 8, description: 'Metric value (e.g., 10K+)' },
            line1: { type: 'string', minLength: 2, maxLength: 22, description: 'First line of label' },
            line2: { type: 'string', minLength: 0, maxLength: 22, description: 'Second line of label' },
            description: { type: 'string', minLength: 10, maxLength: 140, description: 'Metric description' }
          },
          required: ['value', 'line1', 'line2', 'description'],
          additionalProperties: false
        }
      }
    }, ['title', 'leftTitle', 'leftBody', 'website', 'metrics']),
  },
  {
    id: 'swift:timeline',
    name: 'Timeline',
    description: 'Timeline of cards with title, subtitle banner',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 8, maxLength: 60, description: 'Main title' },
      subtitle: { type: 'string', minLength: 20, maxLength: 200, description: 'Subtitle text' },
      items: {
        type: 'array',
        minItems: 1,
        maxItems: 4,
        description: 'Timeline items with year, heading, body, and icon',
        items: {
          type: 'object',
          properties: {
            year: { type: 'string', minLength: 3, maxLength: 6, description: 'Year' },
            heading: { type: 'string', minLength: 3, maxLength: 28, description: 'Item heading' },
            body: { type: 'string', minLength: 10, maxLength: 160, description: 'Item body text' },
            icon: {
              type: 'object',
              properties: {
                __icon_url__: { type: 'string', description: 'URL to icon' },
                __icon_query__: { type: 'string', minLength: 0, maxLength: 80, description: 'Query used to search the icon' }
              },
              required: ['__icon_url__', '__icon_query__'],
              additionalProperties: false
            }
          },
          required: ['year', 'heading', 'body', 'icon'],
          additionalProperties: false
        }
      },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' }
    }, ['title', 'subtitle', 'items', 'website']),
  },
  {
    id: 'swift:table-or-chart',
    name: 'Table or Chart',
    description: 'Swift: Generic data table with option to render a chart (bar, horizontalBar, line, pie)',
    templateID: 'swift',
    templateName: 'swift',
    json_schema: jsonSchema({
      title: { type: 'string', minLength: 6, maxLength: 60, description: 'Main title' },
      description: { type: 'string', minLength: 20, maxLength: 220, description: 'Description text' },
      mode: { type: 'string', enum: ['table', 'chart'], description: 'Render mode' },
      columns: {
        type: 'array',
        minItems: 2,
        maxItems: 10,
        description: 'Table column headers',
        items: { type: 'string', minLength: 1, maxLength: 40 }
      },
      rows: {
        type: 'array',
        minItems: 1,
        maxItems: 30,
        description: 'Table rows',
        items: {
          type: 'object',
          properties: {
            cells: {
              type: 'array',
              minItems: 2,
              maxItems: 10,
              description: 'Row cells; count should match columns length',
              items: { type: 'string', minLength: 0, maxLength: 200 }
            }
          },
          required: ['cells'],
          additionalProperties: false
        }
      },
      chart: {
        type: 'object',
        properties: {
          type: { type: 'string', enum: ['bar', 'horizontalBar', 'line', 'pie'], description: 'Chart type' },
          data: {
            type: 'array',
            minItems: 3,
            maxItems: 12,
            description: 'Chart data points',
            items: {
              type: 'object',
              properties: {
                label: { type: 'string', minLength: 1, maxLength: 12, description: 'Data label' },
                value: { type: 'number', minimum: 0, maximum: 1000000, description: 'Data value' }
              },
              required: ['label', 'value'],
              additionalProperties: false
            }
          },
          primaryColor: { type: 'string', description: 'Primary color for chart' },
          gridColor: { type: 'string', description: 'Grid color for chart' },
          pieColors: {
            type: 'array',
            minItems: 1,
            maxItems: 10,
            description: 'Colors for pie chart segments',
            items: { type: 'string' }
          },
          showLabels: { type: 'boolean', description: 'Show labels on chart' }
        },
        required: ['type', 'data', 'primaryColor', 'gridColor', 'pieColors', 'showLabels'],
        additionalProperties: false
      },
      website: { type: 'string', minLength: 6, maxLength: 60, description: 'Website URL' }
    }, ['title', 'description', 'mode', 'columns', 'rows', 'chart', 'website']),
  },
];

// ─── Registry map ──────────────────────────────────────────────────────────────

export const LAYOUT_GROUPS: Record<string, LayoutGroup> = {
  general: {
    name: 'general',
    ordered: false,
    slides: GENERAL_SLIDES,
  },
  // modern: {
  //   name: 'modern',
  //   ordered: false,
  //   slides: MODERN_SLIDES,
  // },
  // standard: {
  //   name: 'standard',
  //   ordered: false,
  //   slides: STANDARD_SLIDES,
  // },
  swift: {
    name: 'swift',
    ordered: false,
    slides: SWIFT_SLIDES,
  },
};

/**
 * Get the layout group definition for use in the Presenton /prepare payload.
 * Falls back to 'general' if the requested group is not found.
 */
export function getLayoutGroup(group: string): LayoutGroup {
  return LAYOUT_GROUPS[group] ?? LAYOUT_GROUPS['general'];
}

/**
 * Get all slide layout entries for a given group.
 */
export function getLayoutSlides(group: string): SlideLayoutEntry[] {
  return getLayoutGroup(group).slides;
}

/**
 * Get a specific slide layout entry by its full id (e.g. "general:basic-info-slide").
 */
export function getSlideLayout(id: string): SlideLayoutEntry | undefined {
  const [group] = id.split(':');
  return getLayoutSlides(group).find(s => s.id === id);
}

export default LAYOUT_GROUPS;
