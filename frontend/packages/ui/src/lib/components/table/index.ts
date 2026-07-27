import Root from './table.svelte';
import Header from './table-header.svelte';
import Body from './table-body.svelte';
import Footer from './table-footer.svelte';
import Row from './table-row.svelte';
import Head from './table-head.svelte';
import Cell from './table-cell.svelte';
import Caption from './table-caption.svelte';

export const Table = { Root, Header, Body, Footer, Row, Head, Cell, Caption };

export {
	Root,
	Header,
	Body,
	Footer,
	Row,
	Head,
	Cell,
	Caption,
	//
	Root as TableRoot,
	Header as TableHeader,
	Body as TableBody,
	Footer as TableFooter,
	Row as TableRow,
	Head as TableHead,
	Cell as TableCell,
	Caption as TableCaption,
};
