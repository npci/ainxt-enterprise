// SPDX-License-Identifier: Apache-2.0
function HoverTooltip({ id, placement, title, body, visible }) {
    if (!visible || !body) return null;
    return (
        <div
            id={id}
            className={`hover-tooltip hover-tooltip--${placement}`}
            role="tooltip"
        >
            <div className="hover-tooltip__arrow" aria-hidden="true" />
            {title && <div className="hover-tooltip__title">{title}</div>}
            <div className="hover-tooltip__body">{body}</div>
        </div>
    );
}

export default HoverTooltip;
