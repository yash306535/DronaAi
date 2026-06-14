import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "@/app/App";

describe("App", () => {
  it("renders the DRONA AI brand wordmark", () => {
    render(<App />);
    expect(screen.getByText("DRONA AI")).toBeInTheDocument();
  });

  it("redirects the index route to the login page", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: "Login" }),
    ).toBeInTheDocument();
  });
});
