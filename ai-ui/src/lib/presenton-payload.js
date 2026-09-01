// SPDX-License-Identifier: Apache-2.0
// presenton-payload.js
// Build canonical prepare payload for Presenton from the app's outline + options

export const DEFAULT_PRESENTON_TEMPLATE_GROUPS = [
  { id: 'general', name: 'General', color: '#1A2744', preview: 'dark' },
  { id: 'modern', name: 'Modern', color: '#1A73E8', preview: 'light' },
  { id: 'standard', name: 'Standard', color: '#374151', preview: 'light' },
  { id: 'swift', name: 'Swift', color: '#7C3AED', preview: 'dark' },
];

/**
 * Build create payload for POST /api/v1/ppt/presentation/create
 * 
 * @param {string} prompt - The presentation topic/content
 * @param {object} options - Configuration options
 * @returns {object} - Create payload
 */
export function buildCreatePayload(prompt, options = {}) {
  return {
    content: prompt,
    n_slides: options.n_slides || 8,
    language: options.language || 'English',
    instructions: options.instructions || null,
    title: options.title || null,
    tone: options.tone || 'default',
    file_paths: [],
    verbosity: options.verbosity || 'standard',
    user_id: options.user_id || 'anonymous',
    outlines: null,
    include_table_of_contents: options.include_table_of_contents || false,
    include_title_slide: options.include_title_slide || false,
    user_role: options.user_role || null,
    web_search: options.web_search || false,
    layout: null
  };
}

/**
 * Build export payload for POST /api/export-as-pptx or /api/export-as-pdf
 * 
 * @param {string} presentationId - The presentation ID
 * @param {string} title - The presentation title
 * @returns {object} - Export payload
 */
export function buildExportPayload(presentationId, title) {
  return {
    id: presentationId,
    title: title || 'Presentation',
    userId: null,
    role: null
  };
}

/**
 * Maps registry layout to Presenton expected format
 * The registry now contains the exact json_schema, templateID, and templateName
 * 
 * @param {object} layout - Layout from local registry (TemplateGroup)
 * @returns {object} - Layout formatted for Presenton API
 */
function mapLayoutForPresenton(layout) {
  if (!layout || !layout.slides) {
    throw new Error('Invalid layout: must have slides array');
  }

  // Map slides to the exact format Presenton expects
  const mappedSlides = layout.slides.map(slide => {
    // The registry now has the exact format with json_schema, templateID, templateName
    return {
      id: slide.id,
      name: slide.name,
      description: slide.description,
      json_schema: slide.json_schema,
      templateID: slide.templateID || layout.name,
      templateName: slide.templateName || layout.name
    };
  });

  return {
    name: layout.name,
    ordered: layout.ordered !== undefined ? layout.ordered : false,
    slides: mappedSlides
  };
}

/**
 * Build the canonical prepare payload for POST /api/v1/ppt/presentation/prepare
 * 
 * @param {object} outline - { title, slides: [{title, bullets, chart, stats}] }
 * @param {object} options - { n_slides, tone, language, verbosity, template, ... }
 * @param {string|null} presentationId - existing presentation id (for re-prepare)
 * @param {object|null} layoutOverride - full layout object to use (from local registry)
 * @returns {object} - Prepare payload
 */
export function buildPreparePayload(outline, options, presentationId = null, layoutOverride = null) {
  if (!layoutOverride) {
    throw new Error('Layout is required. Please provide layout from local registry.');
  }

  // Build markdown content from outline - one outline per slide
  const outlines = (outline?.slides || []).map((s, i) => {
    const lines = [];
    lines.push(`# ${s.title || ""}`);
    lines.push(''); // Empty line after title
    (s.bullets || []).forEach(b => lines.push(`- ${b}`));
    if (s.chart && s.chart.type && s.chart.type !== 'none') {
      lines.push('');
      lines.push(`Chart: ${s.chart.type} ${s.chart.title || ''}`);
      (s.chart.labels || []).forEach((lbl, li) => lines.push(`  - ${lbl}: ${s.chart.values?.[li] ?? ''}`));
    }
    (s.stats || []).forEach(st => {
      lines.push('');
      lines.push(`Metric: ${st.value} — ${st.label}${st.delta ? ' (' + st.delta + ')' : ''}`);
    });
    return { content: lines.join('\n') };
  });

  // Map layout to Presenton format
  const mappedLayout = mapLayoutForPresenton(layoutOverride);

  const body = {
    ...(presentationId ? { presentation_id: presentationId } : {}),
    outlines,
    layout: mappedLayout
  };

  return body;
}

/**
 * Build update payload for PATCH /api/v1/ppt/presentation/update
 * 
 * @param {string} presentationId - The presentation ID
 * @param {string} title - The presentation title
 * @param {number} nSlides - Number of slides
 * @param {Array} slides - Array of slide objects with content
 * @param {string} userId - User ID for authentication
 * @returns {object} - Update payload
 */
export function buildUpdatePayload(presentationId, title, nSlides, slides, userId) {
  return {
    id: presentationId,
    title: title,
    n_slides: nSlides,
    slides: slides || [],
    user_id: userId
  };
}

/**
 * Get the default website value for slides
 * This is used when generating slide content
 * @returns {string} - Default website URL
 */
export function getDefaultWebsite() {
  return 'https://<YOUR_BASE_URL>';
}

/**
 * Build slide content based on slide type and outline data
 * This helps map generic outline data to specific slide schemas
 * 
 * @param {string} slideType - The slide layout ID (e.g., 'swift:IntroSlideLayout')
 * @param {object} slideData - The outline data for this slide
 * @param {number} index - Slide index
 * @param {number} total - Total slides
 * @returns {object} - Slide content matching the schema
 */
export function buildSlideContent(slideType, slideData, index, total) {
  const website = getDefaultWebsite();
  const title = slideData.title || '';
  const bullets = slideData.bullets || [];
  
  switch (slideType) {
    case 'swift:IntroSlideLayout':
      return {
        title: title,
        subtitlePrefix: 'Presentation',
        subtitleAccent: 'Overview',
        paragraph: bullets.slice(0, 2).join(' ') || 'Introduction slide',
        website: website,
        introCard: {
          enabled: index === 0,
          name: 'AiNxt',
          date: new Date().toLocaleDateString()
        },
        media: {
          type: 'image',
          image: {
            __image_url__: '',
            __image_prompt__: `Professional business presentation cover image for: ${title}`
          }
        }
      };
      
    case 'swift:simple-bullet-points-layout':
      return {
        title: title,
        statement: bullets.slice(0, 2).join(' ') || 'Key points',
        points: bullets.slice(0, 4).map((b, i) => ({
          title: `Point ${i + 1}`,
          body: b
        })),
        website: website
      };
      
    case 'swift:MetricsNumbers':
      return {
        title: title,
        leftTitle: 'Key Metrics',
        leftBody: bullets[0] || 'Performance indicators',
        website: website,
        metrics: (slideData.stats || bullets.slice(0, 4)).map((s, i) => ({
          value: typeof s === 'object' ? s.value : `${(i + 1) * 25}`,
          line1: typeof s === 'object' ? s.label : `Metric ${i + 1}`,
          line2: '',
          description: typeof s === 'object' ? s.value : s
        }))
      };
      
    case 'swift:SwiftTableOfContents':
      return {
        title: 'Table of Contents',
        items: bullets.slice(0, 10).map((b, i) => ({
          title: b,
          description: ''
        })),
        website: website
      };
      
    case 'swift:Timeline':
      return {
        title: title,
        subtitle: bullets[0] || 'Timeline of events',
        items: bullets.slice(0, 4).map((b, i) => ({
          year: `${2020 + i}`,
          heading: `Phase ${i + 1}`,
          body: b,
          icon: {
            __icon_url__: '',
            __icon_query__: 'calendar'
          }
        })),
        website: website
      };
      
    case 'swift:bullet-with-icons-title-description':
      return {
        title: title,
        sideHeading: 'Overview',
        sideParagraph: bullets[0] || '',
        items: bullets.slice(0, 4).map((b, i) => ({
          icon: {
            __icon_url__: '',
            __icon_query__: 'check'
          },
          title: `Item ${i + 1}`,
          description: b
        })),
        website: website
      };
      
    case 'swift:icon-bullet-list-description-slide':
      return {
        title: title,
        description: bullets[0] || '',
        features: bullets.slice(1, 5).map((b, i) => ({
          title: `Feature ${i + 1}`,
          body: b,
          icon: {
            __icon_url__: '',
            __icon_query__: 'star'
          }
        })),
        website: website
      };
      
    case 'swift:image-list-description-slide':
      return {
        titleLine1: title,
        titleLine2: '',
        description: bullets[0] || '',
        items: bullets.slice(1, 7).map((b, i) => ({
          title: `Item ${i + 1}`,
          description: b,
          image: {
            __image_url__: '',
            __image_prompt__: `Image for: ${b}`
          }
        })),
        website: website
      };
      
    case 'swift:tableorChart':
      return {
        title: title,
        description: bullets[0] || '',
        mode: 'table',
        columns: ['Item', 'Description'],
        rows: bullets.slice(1).map((b, i) => ({
          cells: [`${i + 1}`, b]
        })),
        chart: {
          type: 'bar',
          data: bullets.slice(1, 6).map((b, i) => ({
            label: `${i + 1}`,
            value: (i + 1) * 10
          })),
          primaryColor: '#1A73E8',
          gridColor: '#E5E7EB',
          pieColors: ['#1A73E8', '#34D399', '#F59E0B', '#EF4444'],
          showLabels: true
        },
        website: website
      };
      
    default:
      // Generic fallback
      return {
        title: title,
        content: bullets.join('\n'),
        website: website
      };
  }
}
