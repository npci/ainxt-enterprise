// SPDX-License-Identifier: Apache-2.0
/**
 * Presenton Layout Mapping for AiNxt
 * 
 * This file maps AiNxt slide types to Presenton layout IDs.
 * Use this when preparing payloads for the Presenton /prepare API.
 */

// ─── Swift Template Layouts ────────────────────────────────────────────────────

export const SWIFT_LAYOUT_MAP = {
  'intro': 'intro-slide',
  'table_of_contents': 'table-of-contents',
  'simple_bullets': 'simple-bullets',
  'bullets_with_icons': 'bullets-with-icons-title-description',
  'metrics': 'metrics-numbers',
  'timeline': 'timeline',
  'table_or_chart': 'table-or-chart',
} as const;

export type SwiftLayoutType = keyof typeof SWIFT_LAYOUT_MAP;

// ─── Modern Template Layouts ───────────────────────────────────────────────────

export const MODERN_LAYOUT_MAP = {
  'intro': 'intro-slide',
  'about_company': 'about-company-slide',
  'problem': 'problem-slide',
  'solution': 'solution-slide',
  'product_overview': 'product-overview-slide',
  'market_size': 'market-size-slide',
  'market_validation': 'market-validation-slide',
  'company_traction': 'company-traction-slide',
  'business_model': 'business-model-slide',
  'team': 'team-slide',
  'thank_you': 'thank-you-slide',
} as const;

export type ModernLayoutType = keyof typeof MODERN_LAYOUT_MAP;

// ─── Standard Template Layouts ─────────────────────────────────────────────────

export const STANDARD_LAYOUT_MAP = {
  'intro': 'intro-slide',
  'table_of_contents': 'table-of-contents',
  'heading_bullet_image': 'heading-bullet-image',
  'icon_bullets': 'icon-bullets',
  'metrics_description': 'metrics-description',
  'chart_left_text_right': 'chart-left-text-right-layout',
  'icon_bullet_description': 'icon-bullet-description',
  'contact': 'contact',
} as const;

export type StandardLayoutType = keyof typeof STANDARD_LAYOUT_MAP;

// ─── General Template Layouts ──────────────────────────────────────────────────

export const GENERAL_LAYOUT_MAP = {
  'intro': 'general-intro-slide',
  'basic_info': 'basic-info-slide',
  'bullet_icons_only': 'bullet-icons-only-slide',
  'bullet_with_icons': 'bullet-with-icons-slide',
  'chart_with_bullets': 'chart-with-bullets-slide',
  'metrics': 'metrics-slide',
  'metrics_with_image': 'metrics-with-image-slide',
  'numbered_bullets': 'numbered-bullets-slide',
  'quote': 'quote-slide',
  'table_info': 'table-info-slide',
  'table_of_contents': 'table-of-contents-slide',
  'team': 'team-slide',
} as const;

export type GeneralLayoutType = keyof typeof GENERAL_LAYOUT_MAP;

// ─── Template Group to Layout Map ──────────────────────────────────────────────

export const TEMPLATE_LAYOUT_MAPS = {
  swift: SWIFT_LAYOUT_MAP,
  modern: MODERN_LAYOUT_MAP,
  standard: STANDARD_LAYOUT_MAP,
  general: GENERAL_LAYOUT_MAP,
} as const;

export type TemplateGroup = keyof typeof TEMPLATE_LAYOUT_MAPS;
export type LayoutType = SwiftLayoutType | ModernLayoutType | StandardLayoutType | GeneralLayoutType;

// ─── Helper Functions ──────────────────────────────────────────────────────────

/**
 * Get the layout map for a specific template group
 * @param templateGroup - The template group (swift, modern, standard, general)
 * @returns The layout map for that group
 */
export function getLayoutMap(templateGroup: TemplateGroup) {
  return TEMPLATE_LAYOUT_MAPS[templateGroup];
}

/**
 * Get the Presenton layout ID for a given template group and slide type
 * @param templateGroup - The template group (swift, modern, standard, general)
 * @param slideType - Your internal slide type
 * @returns The Presenton layout ID
 */
export function getPresentonLayoutId(
  templateGroup: TemplateGroup,
  slideType: LayoutType
): string {
  const map = TEMPLATE_LAYOUT_MAPS[templateGroup];
  return (map as Record<string, string>)[slideType];
}

/**
 * Get the full layout key for Presenton API
 * @param templateGroup - e.g., 'swift', 'modern', 'standard', 'general'
 * @param layoutId - the layout ID from the map
 * @returns full layout key like 'swift:simple-bullets'
 */
export function getPresentonLayoutKey(
  templateGroup: string, 
  layoutId: string
): string {
  return `${templateGroup}:${layoutId}`;
}

/**
 * Create a slide object for Presenton API
 * @param templateGroup - The template group (swift, modern, standard, general)
 * @param slideType - Your internal slide type
 * @param content - The slide content data
 * @returns A slide object ready for Presenton API
 */
export function createPresentonSlide<T extends TemplateGroup>(
  templateGroup: T,
  slideType: keyof typeof TEMPLATE_LAYOUT_MAPS[T],
  content: Record<string, any>
): {
  layout_group: string;
  layout: string;
  content: Record<string, any>;
} {
  const map = TEMPLATE_LAYOUT_MAPS[templateGroup];
  return {
    layout_group: templateGroup,
    layout: (map as Record<string, string>)[slideType as string],
    content,
  };
}

// ─── Example Usage ─────────────────────────────────────────────────────────────
/*
import { createPresentonSlide, SWIFT_LAYOUT_MAP, getPresentonLayoutId } from './presenton-layouts';

// Create a slide for the API payload
const slide = createPresentonSlide('swift', 'simple_bullets', {
  title: 'Our Commitment',
  statement: 'We are committed to excellence...',
  points: [
    { title: 'Point 1', body: 'Description 1' },
    { title: 'Point 2', body: 'Description 2' }
  ],
  website: 'www.example.com'
});

// Or get layout ID directly
const layoutId = getPresentonLayoutId('swift', 'simple_bullets'); // 'simple-bullets'

// Use in your API call
const payload = {
  template: 'swift',
  slides: [slide]
};
*/
