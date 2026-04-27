---
name: spintax-rules-spam-words
description: Mail Meteor spam trigger word list. Load only during Step 4 (spam word check) of the spintax pipeline — large file, keep out of generation context.
type: reference
tags: [spintax, rules, spam-words]
status: active
created: "2026-04-23"
---

# Spam Trigger Words

Source: Mail Meteor's 2026 spam words guide (https://mailmeteor.com/blog/spam-words).

These trigger Gmail / Outlook / Apple Mail spam filters.

## Usage rules

- **Never in subject lines.** Zero exceptions.
- **In the body:** use only once per email, and only if the word is operationally required (e.g., a product name or industry term).
- **When spinning variations:** replace these words with neutral equivalents unless the meaning depends on them.
- **Two triggers in the same sentence → rewrite.** Even load-bearing triggers should not stack.

## Exception handling

Legitimate B2B cold email sometimes needs spam-triggering industry terms. For fintech, lending, legal, insurance, and similar verticals you may need words like:

- `revenue`, `bank`, `mortgage`, `insurance`, `loan`, `credit`

When a trigger is load-bearing:

1. Use it only once per email, and NEVER in the subject line.
2. Never combine two triggers in a single sentence.
3. Run the final copy through the `emailguard-spam-check` skill before activating drafts.
4. Flag the deliberately-kept trigger in the pipeline delivery note.

---

## Money & Financial

$$$, €€€, £££, 50% off, A few bob, Accept cash cards, Accept credit cards, Affordable, Affordable deal, Avoid bankruptcy, Bad credit, Bank, Bankruptcy, Bargain, Billing, Billing address, Billion, Billion dollars, Billionaire, Card accepted, Cards accepted, Cash, Cash bonus, Cash out, Cash-out, Cashcashcash, Casino, Cents on the dollar, Check, Check or money order, Claim your discount, Cost, Costs, Credit, Credit bureaus, Credit card, Credit card offers, Credit or Debit, Deal, Debt, Discount, Dollars, Double your, Double your wealth, Earn, Earn $, Earn cash, Earn extra income, Earn from home, Earn monthly, Earn per month, Earn per week, Earn per year, Easy income, Easy terms, F r e e, For free, For just $, Free access, Free consultation, Free gift, Free hosting, Free info, Free investment, Free membership, Free money, Free preview, Free quote, Free trial, Full refund, Get out of debt, Giveaway, Guaranteed deposit, Increase revenue, Increase sales/traffic, Instant earnings, Instant income, Insurance, Investment, Investment advice, Loans, Make $, Money-back guarantee, Mortgage, Mortgage rates, Offer, One hundred percent free, Only $, Price, Price protection, Profits, Quote, Refinance, Save $, Save big money, Subject to credit, US Dollars, Why pay more?, Your income

## Scam / Too-Good-to-Be-True

100% guaranteed, Access now, Act fast, Amazing deal, Apply now, As seen on, Best deal, Big profit, Can't miss, Click below, Click here, Deal ending soon, Don't delete, Double your money, Exclusive deal, Fantastic offer, Free membership, Get it now, Great news, Guaranteed results, Important information, Increase sales, Instant savings, Limited time, Must read, New customers only, No catch, No cost, No credit check, No obligation, No strings attached, Once in a lifetime, Only available here, Order now, Potential earnings, Pure profit, Risk-free, Special invitation, Special offer, This won't last, Urgent, Will not believe

## Marketing Overpromise

#1, 100% free, 100% off, 100% satisfied, Additional income, Amazed, Amazing, Amazing deal, Amazing offer, Amazing stuff, Be amazed, Be surprised, Be your own boss, Best bargain, Best deal, Best offer, Best price, Best rates, Big bucks, Bonus, Can't live without, Consolidate debt, Double your cash, Double your income, Drastically reduced, Earn extra cash, Earn money, Expect to earn, Extra, Extra cash, Extra income, Fantastic, Fantastic deal, Fantastic offer, Fast cash, Financial freedom, Free priority mail, Get paid, Incredible deal, Join millions, Lowest price, Make money, Million dollars, Money-back guarantee, Prize, Promise, Pure profit, Risk-free, Satisfaction guaranteed, Save up to, Special promotion, The best, Thousands, Unbeatable offer, Unbelievable, Unlimited, Wonderful, You will not believe your eyes

## Urgency / Clickbait / Pressure

Access, Access now, Act, Act immediately, Act now, Act now!, Action, Action required, Apply here, Apply now, Apply now!, Apply online, Become a member, Before it's too late, Being a member, Buy, Buy direct, Buy now, Buy today, Call, Call free, Call free/now, Call me, Call now, Call now!, Can we have a minute of your time?, Cancel now, Cancellation required, Claim now, Click, Click below, Click here, Click me to download, Click now, Click this link, Click to get, Click to remove, Contact us immediately, Deal ending soon, Do it now, Do it today, Don't delete, Don't hesitate, Don't waste time, Exclusive deal, Expire, Expires today, Final call, For instant access, For Only, For you, Friday before [holiday], Get it away, Get it now, Get now, Get paid, Get started, Get started now, Great offer, Hurry up, Immediately, Info you requested, Information you requested, Instant, Limited time, New customers only, Now, Now only, Offer expires, Once in lifetime, Only, Order now, Order today, Please read, Purchase now, Sign up free, Sign up free today, Supplies are limited, Take action, Take action now, This won't last, Time limited, Today, Top urgent, Trial, Urgent, What are you waiting for?, While supplies last, You are a winner

## Health & Pharma

100% natural, All natural, Best price, Certified organic, Clinical trial, Cure for, Diet pill, Doctor recommended, Double blind study, Fat burner, Fast weight loss, Free consultation, Get slim, Guaranteed weight loss, Hair growth, Lose weight fast, Medical breakthrough, Miracle cure, Money-back guarantee, Natural remedy, No prescription needed, Online pharmacy, Over-the-counter, Pain relief, Prescription drugs, Reverse aging, Safe and effective, Scientifically proven, Secret formula, Weight loss, Youthful skin

## Tech / Security (Phishing Triggers)

Access your account, Account update, Action required, Activate now, Antivirus, Change password, Click to verify, Confirm your details, Confidential information, Cyber Monday, Data breach, Download now, Final notice, Free antivirus, Free trial, Important update, Immediate action required, Improve security, Install now, Last warning, Log in now, New login detected, Online account, Password reset, Payment details needed, Phishing alert, Secure payment, Security breach, Security update, Update account, Verify identity, Warning message

## Gambling / Adult / Blacklisted

Adult content, Bet now, Big win, Blackjack, Casino bonus, Cash out now, Click to win, Double your money, Exclusive access, Free chips, Free spins, Gamble online, Hot deal, Instant winnings, Jackpot, Live dealer, Lottery winner, Lucky chance, Online betting, Online casino, Online gaming, Poker tournament, Risk-free bet, Slots jackpot, Spin to win, Try for free, VIP offer, Winner announced, Winning numbers, XXX
