"""Storefront demo copy: journal articles and static pages, shared by
seed_storefront_demo.py (fresh seeds) and the dev-DB content updater.

PROVISIONAL content for the demo/storefront. The contact details and the
compliance identifiers below (GSTIN, FSSAI licence, Grievance Officer,
registered address) are DUMMY placeholders — replace them with the real
registered values, and have the Terms & Privacy text reviewed by a lawyer,
before showing this to real customers.
"""
import datetime as dt

# --- brand / contact / compliance (DUMMY — replace before launch) ---
CONTACT = {
    "email": "care@drsdairy.in",
    "phone": "+91 90000 00000",
    "hours": "7:00 am to 8:00 pm, every day",
    "entity": "Dhanna Ram Sherawat Enterprises",
    "gstin": "08ABCDE1234F1Z5",
    "fssai": "10024051001234",
    "grievance_officer": "Rohit Sherawat",
    "address": "Plot 12, Dairy Road, Bani Park, Jaipur, Rajasthan 302016, India",
}

_UPDATED = "25 October 2026"

# ------------------------------------------------------------------ ARTICLES
# kind = category used for the (fallback) cover art / photo mapping.
ARTICLES = [
    {
        "slug": "why-bilona-ghee", "kind": "ghee", "category": "heritage",
        "title": "Why Bilona Ghee is different",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 8, 19, 6, 30, tzinfo=dt.UTC),
        "tags": ["ghee", "heritage", "bilona"],
        "excerpt": "The traditional bilona method — curd-churned, not cream-boiled — and why you can taste it.",
        "body_html": """
<p>Not all ghee is made the same way — and with bilona ghee, the method is the whole point.</p>
<h2>What "bilona" actually means</h2>
<p>Bilona is the traditional hand-churning method. Instead of boiling cream straight into ghee, we first set whole milk into curd, churn that curd to lift out cultured butter, and only then simmer the butter slowly into ghee. It's more work and a lower yield — but it's what gives bilona ghee its deep aroma, golden grain and clean taste.</p>
<h2>Curd-churned, not cream-boiled</h2>
<p>Most mass-market ghee is made directly from cream. Churning from curd instead develops flavour and makes the ghee easier to digest for many people. You can smell the difference the moment the jar opens.</p>
<h2>Why we use A2 milk</h2>
<p>We make our bilona ghee from the milk of native cows, prized for its richness. Combined with slow churning, it's ghee the way it was made in Indian kitchens for generations — nothing added, nothing rushed.</p>
<p>Spoon it over hot dal and rice, or use it for tempering — a little goes a long way.</p>
""",
    },
    {
        "slug": "from-farm-to-your-doorstep", "kind": "milk", "category": "our-story",
        "title": "From the farm to your doorstep before breakfast",
        "author_name": "DRS Team", "published": dt.datetime(2026, 9, 2, 6, 30, tzinfo=dt.UTC),
        "tags": ["milk", "delivery"],
        "excerpt": "How a morning's milk travels from partner farms to your door, chilled all the way.",
        "body_html": """
<p>By the time you're pouring your morning chai, your milk has already had a busy few hours.</p>
<h2>Milked at dawn</h2>
<p>Fresh milk is collected from our partner farms early every morning, where the animals are well cared for and the milking is done hygienically.</p>
<h2>Tested and chilled, fast</h2>
<p>At the dairy, every batch is checked for quality and purity before anything is packed. Milk is chilled quickly and stays cold through every step — because the cold chain is what keeps it tasting just-milked.</p>
<h2>At your door before 7&nbsp;am</h2>
<p>Packed and routed overnight, your order is delivered chilled, before most of the city is awake. Fresh isn't a label for us — it's the whole operation, built around one simple promise: good milk, on time, every single day.</p>
""",
    },
    {
        "slug": "what-pure-milk-means-to-us", "kind": "milk", "category": "quality",
        "title": "What 'pure milk' means to us",
        "author_name": "DRS Quality Team", "published": dt.datetime(2026, 9, 16, 6, 30, tzinfo=dt.UTC),
        "tags": ["quality", "milk"],
        "excerpt": "The checks every batch goes through before it is packed.",
        "body_html": """
<p>"Pure" is an easy word to print on a carton. Here's what it actually means at DRS.</p>
<h2>Nothing added</h2>
<p>No preservatives, no thickeners, no added water, no shortcuts. Our milk is milk — pasteurised for safety and nothing more.</p>
<h2>Tested, every batch</h2>
<p>Before any batch is packed, we check it for fat and SNF, freshness and adulteration. If a batch doesn't meet the mark, it doesn't go out. Every delivery is traceable back to the batch it came from.</p>
<h2>Cold all the way</h2>
<p>Purity isn't only about what's in the milk — it's about keeping it right. We chill quickly and hold the cold chain from the dairy to your doorstep, so what you pour is as good as what we packed.</p>
<p>That's the standard we hold ourselves to, because the dairy carries our founder's grandfather's name.</p>
""",
    },
    {
        "slug": "ghee-the-traditional-way", "kind": "ghee", "category": "process",
        "title": "Ghee, the slow and traditional way",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 9, 30, 6, 30, tzinfo=dt.UTC),
        "tags": ["ghee", "tradition"],
        "excerpt": "Why our ghee takes hours, and why you can smell the difference.",
        "body_html": """
<p>Our ghee takes hours, not minutes — and that's exactly why it smells and tastes the way it does.</p>
<h2>Slow is the secret</h2>
<p>We simmer cultured butter gently and patiently, letting the milk solids caramelise just enough to turn golden and nutty. Rush it on high heat and you lose the aroma; take your time and the whole kitchen knows ghee is being made.</p>
<h2>The grain you can see</h2>
<p>Good traditional ghee sets with a soft, golden grain rather than a flat, oily layer. That texture comes from slow cooking and careful cooling — a small sign of a method done properly.</p>
<h2>How to use it</h2>
<p>Spoon it over khichdi, dal and rotis, use it to temper spices, or add a little to sweets. It has a high smoke point, so it's wonderful for everyday cooking too.</p>
""",
    },
    {
        "slug": "three-summer-coolers-with-chaas", "kind": "buttermilk", "category": "recipes",
        "title": "Three summer coolers with chaas",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 10, 7, 6, 30, tzinfo=dt.UTC),
        "tags": ["recipes", "buttermilk"],
        "excerpt": "Masala chaas, mint chaas and a sweet saffron version for hot afternoons.",
        "body_html": """
<p>Chaas (spiced buttermilk) is the original summer cooler — light, probiotic and endlessly adaptable. Here are three ways we love it.</p>
<h2>1. Classic masala chaas</h2>
<p>Whisk a glass of chilled chaas with a pinch of roasted cumin powder, black salt, a little grated ginger and a few chopped coriander leaves. Serve cold. Nothing beats it after a heavy lunch.</p>
<h2>2. Mint &amp; green-chilli chaas</h2>
<p>Blend a small handful of mint leaves and half a green chilli into your chaas, strain, and finish with black salt. Cooling and just a little fiery.</p>
<h2>3. Sweet saffron chaas</h2>
<p>Prefer it sweet? Stir in a touch of sugar or honey and a few strands of saffron soaked in warm milk. A gentle, fragrant version for hot afternoons.</p>
<p>Start with well-chilled DRS chaas and each one comes together in under five minutes.</p>
""",
    },
    {
        "slug": "easy-homemade-paneer-tikka", "kind": "cheese", "category": "recipes",
        "title": "Easy paneer tikka at home",
        "author_name": "DRS Kitchen", "published": dt.datetime(2026, 10, 14, 6, 30, tzinfo=dt.UTC),
        "tags": ["recipes", "paneer"],
        "excerpt": "Smoky, tender paneer tikka on a regular pan in under 30 minutes.",
        "body_html": """
<p>Smoky, tender paneer tikka — on a regular pan, in under 30 minutes. No tandoor needed.</p>
<h2>You'll need</h2>
<ul>
  <li>250&nbsp;g fresh paneer, cubed</li>
  <li>½ cup thick curd</li>
  <li>1 tsp each ginger-garlic paste, red chilli powder, roasted cumin; ½ tsp garam masala, turmeric and salt to taste</li>
  <li>1 tbsp besan (gram flour), a squeeze of lemon, a little oil</li>
  <li>Onion and capsicum chunks</li>
</ul>
<h2>Method</h2>
<ol>
  <li>Lightly roast the besan in a dry pan, then mix it into the curd with all the spices, lemon and salt to make a thick marinade.</li>
  <li>Fold in the paneer, onion and capsicum. Rest for 15 to 20 minutes.</li>
  <li>Cook on a hot, oiled pan (or skewers over a flame) until charred at the edges, turning so every side colours.</li>
  <li>Finish with chaat masala and lemon; serve with mint chutney.</li>
</ol>
<p>Firm, fresh paneer holds its shape best — which is exactly how ours is made.</p>
""",
    },
]

# ------------------------------------------------------------------ STATIC PAGES
_c = CONTACT
PAGES = {
"about-us": ("About DRS", """
<p>DRS Dairy is named after our founder's late grandfather, <strong>Dhanna Ram Sherawat</strong>. His initials are on every bottle we send out — a promise that what reaches your family is as honest as the people who raised us.</p>
<p>We deliver pure milk, curd, paneer, cheese and traditional bilona ghee — pasteurised at dawn, chilled all the way, and at your door before 7&nbsp;am. No preservatives, no shortcuts, every batch tested.</p>
<p>Read the full story on our <a href="/about">About</a> page, or browse the <a href="/journal">journal</a> to see how we work.</p>
"""),

"contact": ("Contact us", f"""
<p>We'd love to hear from you — whether it's about an order, a subscription, or just a question about how we make our milk and ghee.</p>
<h2>Customer care</h2>
<ul>
  <li><strong>Email:</strong> <a href="mailto:{_c['email']}">{_c['email']}</a></li>
  <li><strong>Phone &amp; WhatsApp:</strong> {_c['phone']}</li>
  <li><strong>Support hours:</strong> {_c['hours']}</li>
</ul>
<p>For the quickest help with a delivery, please keep your order number handy.</p>
<h2>Subscriptions &amp; daily delivery</h2>
<p>You can pause, skip or change any subscription right from your account, up to the daily cut-off. If something didn't arrive as expected, message us the same morning and we'll make it right.</p>
<h2>Business &amp; bulk orders</h2>
<p>Run a café, cloud kitchen or store? We supply fresh dairy on standing daily orders with credit terms. Write to us at <a href="mailto:{_c['email']}">{_c['email']}</a> and we'll set you up.</p>
<h2>Our details</h2>
<p>{_c['entity']}<br/>
Registered address: {_c['address']}<br/>
GSTIN: {_c['gstin']}<br/>
FSSAI licence no.: {_c['fssai']}</p>
"""),

"delivery-areas": ("Delivery areas", f"""
<p>We deliver fresh dairy every morning, before 7&nbsp;am, across our serviced neighbourhoods. More areas are being added as we grow.</p>
<h2>How to check your area</h2>
<p>Enter your delivery pincode at checkout (or on any product page) and we'll tell you right away whether we currently reach you. If we don't yet, you can leave your pincode and we'll notify you the moment we do.</p>
<h2>Delivery timing</h2>
<ul>
  <li>Daily morning delivery, completed before 7&nbsp;am.</li>
  <li>Subscriptions follow a daily cut-off the evening before — order or change before then for next-morning delivery.</li>
  <li>Everything travels chilled, from our dairy to your doorstep.</li>
</ul>
<h2>Not in your area yet?</h2>
<p>We're expanding steadily. Share your pincode with us at <a href="mailto:{_c['email']}">{_c['email']}</a> and we'll prioritise neighbourhoods with the most interest.</p>
"""),

"faq": ("Frequently asked questions", f"""
<h2>Orders &amp; delivery</h2>
<h3>When will my order arrive?</h3>
<p>Daily deliveries are completed before 7&nbsp;am. For next-morning delivery, place or change your order before the evening cut-off shown at checkout.</p>
<h3>Which areas do you deliver to?</h3>
<p>Enter your pincode at checkout to check serviceability. See <a href="/delivery-areas">Delivery areas</a> for more.</p>
<h2>Subscriptions</h2>
<h3>Can I pause or skip a delivery?</h3>
<p>Yes. Pause, skip or change quantities any time before the daily cut-off, right from your account — no call needed.</p>
<h3>How does payment work for subscriptions?</h3>
<p>You can pay per delivery from your wallet, or set up UPI Autopay so it's automatic. We send a pre-debit notice before every auto-payment.</p>
<h2>Payments &amp; pricing</h2>
<h3>What payment methods do you accept?</h3>
<p>UPI, cards and netbanking through our secure payment partner, plus your DRS wallet. All prices include GST.</p>
<h3>Do you store my card details?</h3>
<p>No. Payments are handled by our PCI-compliant payment partner; we never see or store your full card number.</p>
<h2>Quality &amp; returns</h2>
<h3>Are your products really preservative-free?</h3>
<p>Yes — nothing is added to our milk, and every batch is tested before it's packed.</p>
<h3>Something wasn't right with my order.</h3>
<p>Because dairy is perishable, tell us the same morning at <a href="mailto:{_c['email']}">{_c['email']}</a> or {_c['phone']} and we'll refund or replace it. See our <a href="/terms">Terms</a> for the full policy.</p>
"""),

"terms": ("Terms of service", f"""
<p><em>These terms are a working draft and are being finalised with legal review. Last updated {_UPDATED}.</em></p>
<h2>1. Who we are</h2>
<p>This website and the DRS Dairy service are operated by <strong>{_c['entity']}</strong> ("DRS", "we", "us"), GSTIN {_c['gstin']}, FSSAI licence no. {_c['fssai']}. By creating an account or placing an order, you agree to these terms.</p>
<h2>2. Eligibility &amp; your account</h2>
<p>You must be 18 or older to buy from us. You're responsible for keeping your account and phone number secure; orders placed through your verified account are treated as made by you.</p>
<h2>3. Orders &amp; subscriptions</h2>
<p>A subscription is a standing daily order. We prepare each day's deliveries after a daily cut-off; you can pause, skip or change quantities before that cut-off. Orders confirmed after cut-off are delivered the following eligible morning.</p>
<h2>4. Pricing &amp; taxes</h2>
<p>All prices are in Indian Rupees and are inclusive of GST. We may change prices from time to time; the price shown at the time you confirm an order is the price you pay.</p>
<h2>5. Payments</h2>
<p>Payments are processed by our third-party payment partner. For subscriptions you may authorise UPI Autopay / e-mandate, subject to the applicable per-cycle limit; we send a pre-debit notification before each automatic payment, in line with RBI rules.</p>
<h2>6. Delivery</h2>
<p>We deliver to serviceable areas only, generally before 7&nbsp;am. Risk passes to you on delivery to your address. Please ensure someone can receive chilled perishable items.</p>
<h2>7. Cancellations, returns &amp; refunds</h2>
<p>You can cancel or skip before the daily cut-off at no charge. As our products are perishable, we do not accept returns of delivered goods, but if an item is missing, damaged or not of the quality promised, report it the same day and we will refund or replace it. Refunds are made to your original payment method or DRS wallet.</p>
<h2>8. Wallet &amp; credit</h2>
<p>Wallet and credit balances can be used only to pay for DRS orders, are non-transferable and non-withdrawable except as required by law. B2B credit terms, where offered, are governed by your separate agreement with us.</p>
<h2>9. Acceptable use</h2>
<p>Don't misuse the service, attempt to disrupt it, or use it for anything unlawful. We may suspend accounts that abuse promotions, wallets or the ordering system.</p>
<h2>10. Liability</h2>
<p>We take great care with food safety and quality. To the extent permitted by law, our liability for any order is limited to the value of that order. Nothing in these terms limits liability that cannot be limited under applicable law.</p>
<h2>11. Changes &amp; governing law</h2>
<p>We may update these terms; continued use means you accept the changes. These terms are governed by the laws of India, and disputes are subject to the courts of Jaipur, Rajasthan.</p>
<h2>12. Contact</h2>
<p>Questions about these terms? Email <a href="mailto:{_c['email']}">{_c['email']}</a>.</p>
"""),

"privacy": ("Privacy policy", f"""
<p><em>This policy is a working draft aligned with India's Digital Personal Data Protection (DPDP) Act and is being finalised with legal review. Last updated {_UPDATED}.</em></p>
<h2>1. Introduction</h2>
<p><strong>{_c['entity']}</strong> ("DRS") respects your privacy. This policy explains what personal data we collect, why, and the choices and rights you have.</p>
<h2>2. What we collect</h2>
<ul>
  <li><strong>Account &amp; contact:</strong> your name, phone number and delivery addresses.</li>
  <li><strong>Orders:</strong> what you buy, your subscriptions, wallet and payment status (we do not store full card numbers).</li>
  <li><strong>Device &amp; app:</strong> app/push (FCM) tokens and basic device information, to send order updates.</li>
  <li><strong>Consents:</strong> a record of the permissions you give us, per purpose.</li>
</ul>
<h2>3. Why we use it (purpose)</h2>
<p>We use your data to verify your phone by OTP, take and deliver your orders, run your subscriptions, process payments and refunds, provide support, and — only with your consent — send you offers and updates. We ask for consent separately for each purpose, and you can withdraw any consent at any time.</p>
<h2>4. Who we share it with</h2>
<p>We share the minimum necessary with service providers who work for us: our payment partner, SMS/WhatsApp and notification providers, and delivery partners. We do not sell your personal data.</p>
<h2>5. Your rights</h2>
<p>You can access, correct or update your data, withdraw consent, and request deletion of your account and personal data — directly from your account's privacy settings, or by writing to our Grievance Officer below. We'll respond within the timelines required by law.</p>
<h2>6. Retention</h2>
<p>We keep your data only as long as needed to provide the service and to meet legal, tax and accounting obligations, after which it is deleted or anonymised.</p>
<h2>7. Security</h2>
<p>We protect your data with access controls, encryption in transit, and data-minimisation practices. No system is perfectly secure, but we work hard to keep yours safe.</p>
<h2>8. Children</h2>
<p>Our service is intended for adults; we do not knowingly collect data from children.</p>
<h2>9. Grievance Officer &amp; contact</h2>
<p>For any privacy question or request, contact our Grievance Officer, <strong>{_c['grievance_officer']}</strong>, at <a href="mailto:{_c['email']}">{_c['email']}</a>, {_c['entity']}, {_c['address']}.</p>
"""),
}
