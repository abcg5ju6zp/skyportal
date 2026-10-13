/**
 * Catalog queries.
 *
 * RTK Query conversion of the old `FETCH_CATALOG_QUERIES` / `SUBMIT_CATALOG_QUERY`
 * duck. `getCatalogQueries` lists the queries; `submitCatalogQuery` posts a new
 * one and invalidates the `CatalogQuery` tag so the list refetches.
 *
 * The websocket `REFRESH_CATALOG_QUERIES` message is bridged to cache
 * invalidation via `invalidateOnMessage`.
 */
import { skyportalApi } from "../api/skyportalApi";
import { invalidateOnMessage } from "../api/wsInvalidation";

export type CatalogQuery = Record<string, any>;

export const catalogQueryApi = skyportalApi.injectEndpoints({
  endpoints: (build) => ({
    getCatalogQueries: build.query<CatalogQuery[], void>({
      query: () => "api/catalog_queries",
      providesTags: ["CatalogQuery"],
    }),
    getCatalogQuery: build.query<CatalogQuery, number>({
      query: (id) => `api/catalog_queries/${id}`,
      providesTags: ["CatalogQuery"],
    }),
    submitCatalogQuery: build.mutation<unknown, any>({
      query: (data) => ({
        url: "api/catalog_queries",
        method: "POST",
        body: data,
      }),
      invalidatesTags: ["CatalogQuery"],
    }),
    cancelCatalogQuery: build.mutation<unknown, number>({
      query: (id) => ({
        url: `api/catalog_queries/${id}/cancel`,
        method: "POST",
      }),
      invalidatesTags: ["CatalogQuery"],
    }),
  }),
});

// Websocket: old handler refetched catalog queries on REFRESH_CATALOG_QUERIES.
invalidateOnMessage("skyportal/REFRESH_CATALOG_QUERIES", () => [
  "CatalogQuery",
]);

export const {
  useGetCatalogQueriesQuery,
  useGetCatalogQueryQuery,
  useSubmitCatalogQueryMutation,
  useCancelCatalogQueryMutation,
} = catalogQueryApi;
