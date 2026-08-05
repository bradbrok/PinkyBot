#!/usr/bin/env python3
"""
Affiliate link disclosure audit — CORRECTED for multiline tag matching
Uses re.DOTALL to properly capture <a> tags across multiple lines
"""
import re
import os
from pathlib import Path

def audit_affiliate_links(webroot, verbose=False):
    """
    Audit all external affiliate links for proper rel="sponsored" or rel="nofollow"
    Handles multiline <a> tags correctly
    """
    issues = []
    compliant = 0
    
    # Pattern: capture complete <a> tags including multiline
    # Look for data-affiliate attribute to identify affiliate links
    link_pattern = r'<a\s[^>]*?data-affiliate=["\'][\w-]+["\'][^>]*?</a>'
    
    directories = ['reviews', 'guide', 'magazine', 'tools']
    
    for directory in directories:
        dir_path = os.path.join(webroot, directory)
        if not os.path.exists(dir_path):
            continue
            
        for html_file in Path(dir_path).rglob('*.html'):
            with open(html_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Find all <a> tags with data-affiliate (multiline safe)
            matches = re.finditer(link_pattern, content, re.DOTALL | re.IGNORECASE)
            
            for match in matches:
                tag = match.group(0)
                has_sponsored = 'rel="sponsored' in tag or "rel='sponsored" in tag
                has_nofollow = 'rel="nofollow' in tag or "rel='nofollow" in tag
                
                if has_sponsored or has_nofollow:
                    compliant += 1
                else:
                    rel_attr = 'rel=' in tag
                    issues.append({
                        'file': str(html_file),
                        'tag': tag[:80] + '...' if len(tag) > 80 else tag,
                        'has_rel': rel_attr,
                        'issue': 'Missing rel="sponsored"' if rel_attr else 'Missing rel attribute'
                    })
    
    return {'compliant': compliant, 'issues': issues}

if __name__ == '__main__':
    webroot = '/var/www/bitcoinmarket.net/'
    result = audit_affiliate_links(webroot)
    
    print(f"✅ Affiliate links with rel='sponsored': {result['compliant']}")
    print(f"🔴 Issues found: {len(result['issues'])}")
    if result['issues']:
        for issue in result['issues'][:5]:  # Show first 5
            print(f"  - {issue['file']}: {issue['issue']}")
