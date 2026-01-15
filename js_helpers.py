"""
JavaScript helper functions for browser automation.
All functions run in isolated context using page.evaluate() for undetectability.
"""

# Get outer HTML of elements
CSS_GET_HTML = """
(selector) => {
    const elements = document.querySelectorAll(selector);
    return Array.from(elements).map(el => el.outerHTML);
}
"""

XPATH_GET_HTML = """
(xpath) => {
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const elements = [];
    for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        if (node.outerHTML) {
            elements.push(node.outerHTML);
        } else if (node.textContent) {
            elements.push(node.textContent);
        }
    }
    return elements;
}
"""

# Get text content of elements
CSS_GET_TEXT = """
(selector) => {
    const elements = document.querySelectorAll(selector);
    return Array.from(elements).map(el => el.textContent.trim());
}
"""

XPATH_GET_TEXT = """
(xpath) => {
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const elements = [];
    for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        elements.push((node.textContent || '').trim());
    }
    return elements;
}
"""

# Click on elements with nth parameter support
# nth: 0 = first, -1 = last, null = all
CSS_CLICK = """
(args) => {
    const [selector, nth] = args;
    const elements = Array.from(document.querySelectorAll(selector));
    if (elements.length === 0) return [];
    
    let targets;
    if (nth === null || nth === undefined) {
        targets = elements;  // all elements
    } else if (nth === -1) {
        targets = [elements[elements.length - 1]];  // last element
    } else {
        targets = elements[nth] ? [elements[nth]] : [];  // nth element (default 0 = first)
    }
    
    const results = [];
    for (const el of targets) {
        try {
            el.click();
            results.push('clicked');
        } catch (e) {
            results.push('error: ' + e.message);
        }
    }
    return results;
}
"""

XPATH_CLICK = """
(args) => {
    const [xpath, nth] = args;
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const elements = [];
    for (let i = 0; i < result.snapshotLength; i++) {
        elements.push(result.snapshotItem(i));
    }
    if (elements.length === 0) return [];
    
    let targets;
    if (nth === null || nth === undefined) {
        targets = elements;  // all elements
    } else if (nth === -1) {
        targets = [elements[elements.length - 1]];  // last element
    } else {
        targets = elements[nth] ? [elements[nth]] : [];  // nth element (default 0 = first)
    }
    
    const results = [];
    for (const node of targets) {
        try {
            node.click();
            results.push('clicked');
        } catch (e) {
            results.push('error: ' + e.message);
        }
    }
    return results;
}
"""

# Fill elements with value and nth parameter support
# nth: 0 = first, -1 = last, null = all
CSS_FILL = """
(args) => {
    const [selector, value, nth] = args;
    const elements = Array.from(document.querySelectorAll(selector));
    if (elements.length === 0) return [];
    
    let targets;
    if (nth === null || nth === undefined) {
        targets = elements;  // all elements
    } else if (nth === -1) {
        targets = [elements[elements.length - 1]];  // last element
    } else {
        targets = elements[nth] ? [elements[nth]] : [];  // nth element (default 0 = first)
    }
    
    const results = [];
    for (const el of targets) {
        try {
            // Clear existing value
            el.value = '';
            // Dispatch events to simulate real typing
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            results.push('filled');
        } catch (e) {
            results.push('error: ' + e.message);
        }
    }
    return results;
}
"""

XPATH_FILL = """
(args) => {
    const [xpath, value, nth] = args;
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const elements = [];
    for (let i = 0; i < result.snapshotLength; i++) {
        elements.push(result.snapshotItem(i));
    }
    if (elements.length === 0) return [];
    
    let targets;
    if (nth === null || nth === undefined) {
        targets = elements;  // all elements
    } else if (nth === -1) {
        targets = [elements[elements.length - 1]];  // last element
    } else {
        targets = elements[nth] ? [elements[nth]] : [];  // nth element (default 0 = first)
    }
    
    const results = [];
    for (const node of targets) {
        try {
            node.value = '';
            node.focus();
            node.value = value;
            node.dispatchEvent(new Event('input', { bubbles: true }));
            node.dispatchEvent(new Event('change', { bubbles: true }));
            results.push('filled');
        } catch (e) {
            results.push('error: ' + e.message);
        }
    }
    return results;
}
"""

# Get attribute value from elements
# CSS: Use attribute selectors like [href], [data-id], etc.
# For specific attribute extraction, use these templates:
CSS_GET_ATTRIBUTE_TEMPLATE = """
(args) => {{
    const [selector, attrName] = args;
    const elements = document.querySelectorAll(selector);
    return Array.from(elements).map(el => el.getAttribute(attrName) || '');
}}
"""

XPATH_GET_ATTRIBUTE_TEMPLATE = """
(args) => {{
    const [xpath, attrName] = args;
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const elements = [];
    for (let i = 0; i < result.snapshotLength; i++) {{
        const node = result.snapshotItem(i);
        if (node.getAttribute) {{
            elements.push(node.getAttribute(attrName) || '');
        }} else {{
            // For attribute nodes from XPath like @href
            elements.push(node.textContent || node.nodeValue || '');
        }}
    }}
    return elements;
}}
"""

# Direct XPath attribute extraction (for @attribute syntax)
XPATH_GET_ATTRIBUTE_DIRECT = """
(xpath) => {
    const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const values = [];
    for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        // Handle both attribute nodes and element nodes
        values.push(node.nodeValue || node.textContent || '');
    }
    return values;
}
"""


# Helper functions to get the appropriate JS code
def get_html_js(selector_type: str) -> str:
    """Get JS code for extracting HTML content"""
    return CSS_GET_HTML if selector_type == "css" else XPATH_GET_HTML


def get_text_js(selector_type: str) -> str:
    """Get JS code for extracting text content"""
    return CSS_GET_TEXT if selector_type == "css" else XPATH_GET_TEXT


def get_click_js(selector_type: str) -> str:
    """
    Get JS code for clicking elements.
    Accepts args: [selector, nth]
    - nth=0: first element (default)
    - nth=-1: last element
    - nth=null: all elements
    """
    return CSS_CLICK if selector_type == "css" else XPATH_CLICK


def get_fill_js(selector_type: str) -> str:
    """
    Get JS code for filling elements.
    Accepts args: [selector, value, nth]
    - nth=0: first element (default)
    - nth=-1: last element
    - nth=null: all elements
    """
    return CSS_FILL if selector_type == "css" else XPATH_FILL


def get_attribute_js(selector_type: str) -> str:
    """Get JS code for extracting attribute values"""
    return CSS_GET_ATTRIBUTE_TEMPLATE if selector_type == "css" else XPATH_GET_ATTRIBUTE_TEMPLATE


def get_attribute_direct_xpath_js() -> str:
    """Get JS code for direct XPath attribute extraction (e.g., //a/@href)"""
    return XPATH_GET_ATTRIBUTE_DIRECT

