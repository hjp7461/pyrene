-- PLAN-002 Day 1 / PRD-002 L-02: COMMENT ON TABLE for DVD Rental's 15 tables.
--
-- Source: postgresqltutorial.com DVD Rental sample database ER diagram
-- (https://www.postgresqltutorial.com/postgresql-sample-database/).
--
-- These comments feed PRD-002's schema-RAG indexer: the indexer reads them
-- via pg_description, concatenates them with the column list, and embeds the
-- result. Without them the RAG only has table+column names to go on, which
-- top-3 evals (PRD-002 §6) show is not enough for natural-language queries.
--
-- All comments are written in English deliberately — PRD-002 O-04 (Korean
-- description) is still open; we keep one consistent language until then.
--
-- COMMENT ON is idempotent (re-running overwrites with the same body), so
-- this script is safe under initdb's "run once on empty data dir" model and
-- also safe to re-execute manually against a running DB.

COMMENT ON TABLE public.actor IS
  'Actors who appear in films. Linked to films via film_actor (many-to-many). '
  'Each actor row stores a first and last name plus the standard last_update audit timestamp.';

COMMENT ON TABLE public.address IS
  'Postal addresses used by customers, staff, and stores. Each address belongs to '
  'exactly one city via city_id and is referenced 1:N by customer, staff, and store.';

COMMENT ON TABLE public.category IS
  'Genre / category taxonomy applied to films (e.g. "Action", "Comedy"). '
  'Films are joined to categories via the film_category bridge table.';

COMMENT ON TABLE public.city IS
  'Cities used by the address table. Each city belongs to exactly one country via country_id.';

COMMENT ON TABLE public.country IS
  'Countries referenced by the city table. Top of the geographic hierarchy '
  '(country -> city -> address).';

COMMENT ON TABLE public.customer IS
  'Rental customers. Stores first/last name, email, address_id, the home store_id, '
  'an active flag, and creation timestamp. Joined to rental and payment by customer_id.';

COMMENT ON TABLE public.film IS
  'Films available for rental. Holds title, description, release_year, language_id, '
  'rental_duration, rental_rate, length, replacement_cost, rating (G/PG/PG-13/R/NC-17), '
  'and a tsvector fulltext column. Joined to actors (film_actor), categories '
  '(film_category), and physical copies (inventory).';

COMMENT ON TABLE public.film_actor IS
  'Many-to-many bridge between film and actor. Composite key (actor_id, film_id).';

COMMENT ON TABLE public.film_category IS
  'Many-to-many bridge between film and category. Composite key (film_id, category_id). '
  'Used to find films by genre.';

COMMENT ON TABLE public.inventory IS
  'Physical copies of a film at a specific store. Each inventory row represents one '
  'rentable DVD: a film_id, a store_id, and the inventory_id is what the rental '
  'table references.';

COMMENT ON TABLE public.language IS
  'Spoken / subtitle languages a film is offered in. Referenced by film.language_id '
  'and (nullable) film.original_language_id.';

COMMENT ON TABLE public.payment IS
  'Money received from customers for rentals. One payment row records the customer_id, '
  'the staff_id who took it, the rental_id it settles, the amount (numeric), and '
  'payment_date. The canonical table for revenue / sales queries.';

COMMENT ON TABLE public.rental IS
  'Rental events. Each row records inventory_id (the physical copy), customer_id, '
  'staff_id, rental_date, and return_date (nullable while the DVD is still out). '
  'Joined to payment by rental_id. Returning customers who never rented are computed '
  'as customers with no rows here.';

COMMENT ON TABLE public.staff IS
  'Staff members who run the stores. Stores first/last name, address_id, email, '
  'store_id (their assigned store), active flag, username, and password. Joined to '
  'rental and payment by staff_id, and to store via store.manager_staff_id.';

COMMENT ON TABLE public.store IS
  'Rental store locations. Each store has a manager_staff_id (FK to staff) and '
  'address_id (FK to address). Inventory and customers are scoped by store_id.';

-- Column-level comments on a few high-signal columns. These show up in the
-- embedding payload via pg_description and meaningfully boost retrieval on
-- queries like "revenue", "rental price", "rating".
COMMENT ON COLUMN public.payment.amount       IS 'Money the customer paid for the rental, in the store currency (numeric(5,2)).';
COMMENT ON COLUMN public.payment.payment_date IS 'When the payment was recorded. Use this for time-series revenue queries.';
COMMENT ON COLUMN public.film.rental_rate     IS 'Per-rental price of the film (numeric(4,2)).';
COMMENT ON COLUMN public.film.rating          IS 'MPAA-style rating: G, PG, PG-13, R, or NC-17.';
COMMENT ON COLUMN public.rental.return_date   IS 'When the DVD was returned. NULL while the rental is still outstanding.';
COMMENT ON COLUMN public.customer.active      IS 'Whether the customer is currently active (1) or has been deactivated (0).';
